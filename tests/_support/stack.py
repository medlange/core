# SPDX-License-Identifier: Apache-2.0
"""The stack contract: what "the stack is up" is allowed to mean.

WHY THIS FILE EXISTS
--------------------
`tests/_support/skips.py` turns a skip into a failure. That only helps for a dependency
some test already reaches for and already classifies. It does nothing at all for a
dependency that NO test touches -- and an adversarial pass over this repository found
that to be most of the deployment:

    docker stop medos-gateway medos-worker medos-web medos-minio medos-triton medos-tritond
    pytest tests/unit tests/integration/test_api.py tests/integration/test_queue.py \
        -q --require-stack
    -> 233 passed, exit 0, "INFRA SKIPS: 0"

`medos-gateway` is the worker's ONLY route to DICOM
(`MEDOS_DICOMWEB_URL: http://medos-gateway:8043/dicomweb/<tenant>` in
docker-compose.yml). With it stopped the platform cannot fetch or store a single
instance, the viewer cannot load an image, and the inference plane has no Triton. The
strict-mode suite called that healthy, because "strict" only meant "do not skip", and
nothing skipped.

So `--require-stack` now means what it says: the dependencies the selected tests declare
are PROBED, and an unreachable one aborts the run before a single test is allowed to
pass. See `SUITE_DEPENDENCIES` for the declaration and `PROBES` for the checks.

A STATUS CODE IS NOT PROOF
--------------------------
Both incidents in this project's history were a URL that pointed at the wrong thing, and
the second was specifically a service that answered while not being ready. Every probe
below therefore checks the SHAPE of the response and not just `< 400`:

  * the browser origin is an nginx. It used to answer `200 text/html` for
    every path that does not exist, including `/dicom-web/studies`. A probe that accepts
    any 2xx calls that origin a healthy PACS. `_expect` rejects it.
  * `medos-api` splits liveness from readiness on purpose (`/healthz` deliberately does
    not touch the database). The probe reads `/readyz`, because readiness is the claim
    the tests depend on, and the 503-on-`/readyz` incident is the one that hid.

WHAT IS DELIBERATELY NOT PROBED
-------------------------------
`UNCOVERED` names every compose service that has no probe, with the reason. It is not
documentation: `tests/unit/test_stack_contract.py` parses docker-compose.yml and fails
when a service appears that is in neither table, so a new service cannot be added to the
deployment without someone deciding whether the suite depends on it.

ONE PROBE KEY IS NOT A COMPOSE SERVICE
--------------------------------------
`docker`. Three suites shell out to the docker CLI rather than opening a socket -- the
sealed-mode isolation test, and the two probes below that read a container's own
healthcheck -- and `tests/gate/test_capability_reachable.py` reads the DEPLOYMENT's
`MEDOS_CAPABILITY_PROVIDERS` out of `docker inspect`, because what a compose file says is
what a deployment was asked to be and `Config.Env` is what it is. Without a key of its own
a missing CLI or a dead daemon surfaced as a skip from inside the first fixture that
reached for it, halfway through a gate. A dependency that a suite genuinely has belongs in
the preflight, where it aborts the run by name before any test is allowed to pass. The
coverage test is unaffected: it asks whether every compose SERVICE is classified, and
`docker` is not one.

THE HARNESS REACHES THE PACS THE WAY THE PLATFORM DOES, AND NOT OTHERWISE
------------------------------------------------------------------------
Register entry 70. The compose file used to publish `127.0.0.1:8042` for `orthanc` and
call it "the ONE remaining MOS-DATA-006 deviation", and the three endpoint helpers below
defaulted to it. The deviation was never real: `orthanc` joins only the `pacs` network,
`pacs` is `internal: true`, and Docker publishes NO host port for a container whose only
network is internal -- it records the binding in `HostConfig.PortBindings`, starts the
container healthy, and binds nothing. `docker ps` showed `8042/tcp` where every other
service showed `127.0.0.1:P->P`. So those defaults pointed at an address that cannot
exist on a correctly configured deployment, the `orthanc`, `orthanc-e2e` and
`orthanc-rest` probes were STRUCTURALLY unreachable, and a full run reported
`INFRA SKIPS: 29 (orthanc: 29)` -- which reads as a machine that is temporarily down and
was in fact a permanent property of the topology. MOS-REL-012: "An unexecuted acceptance
criterion means the requirement is not satisfied, whatever the code does."

The repair is the one the production code already made. `medos-worker` is configured with
`MEDOS_DICOMWEB_URL: http://medos-gateway:8043/dicomweb/<tenant>` and `tests/gate/` builds
its client the same way, which is why the gate row declares no `orthanc*` key and was
untouched by any of this. Only this harness was left behind. So:

  * `dicomweb_url()` and `e2e_dicomweb_url()` now DEFAULT to the Gateway's DICOMweb root.
    Two shape differences from Orthanc's own root, and both are load-bearing: the path
    carries a tenant segment (`/dicomweb/{t}`, MOS-DATA-007) where `/dicom-web` does not,
    and every request needs `Authorization: Bearer` (MOS-DATA-002 makes the Gateway the
    chokepoint, and MOS-SEC-008 admits anonymous access on `/healthz`, `/readyz` and the
    OpenAPI document and nowhere else). `MEDOS_DICOMWEB_URL` and `MEDOS_E2E_DICOMWEB_URL`
    are still the override they always were; only the defaults moved.

  * DICOMweb has no DELETE verb in the QIDO/WADO/STOW triad, so fixture teardown genuinely
    needs Orthanc's NATIVE REST API, which MOS-DATA-006 forbids exposing: "A deployment in
    which any other container can open a TCP connection to the PACS port is non-conformant,
    whether or not it has a credential." `orthanc_native_rest()` drives it from INSIDE the
    `medos-orthanc` container over that container's own loopback. That is the PACS talking
    to itself, which MOS-DATA-006's sentence does not reach, and it needs no host route, no
    second network and no published port. `orthancteam/orthanc:25.2.0` ships neither curl
    nor wget, so the mechanism is `python3`, which it does ship -- the same substitution,
    for the same reason, that the service's own healthcheck in docker-compose.yml makes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "PROBES",
    "UNCOVERED",
    "SUITE_DEPENDENCIES",
    "COMPOSE_FILE",
    "PACS_CONTAINER",
    "PACS_LOOPBACK_URL",
    "NativeCall",
    "NativeResult",
    "Unready",
    "compose_service_names",
    "dependencies_for_paths",
    "orthanc_native_rest",
    "probe",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "medos" / "deploy" / "compose" / "docker-compose.yml"


class Unready(Exception):
    """A dependency answered, but not in a way that proves it is the thing we need.

    ``dependency`` is the taxonomy key a caller must report this under when it turns the
    failure into a skip. It exists because the one mechanism in this file that shells out
    -- `orthanc_native_rest` -- fails for two unrelated reasons that need two different
    fixes: the docker CLI or its daemon is absent (`docker`), or the PACS container is not
    there (`orthanc-rest`). A message that names the wrong one sends the reader to the
    wrong machine, which is the same class of waste as the stale `127.0.0.1:55433` that
    started this file.
    """

    def __init__(self, message: str, *, dependency: str | None = None) -> None:
        super().__init__(message)
        self.dependency = dependency


# --------------------------------------------------------------------------------------
# Endpoints. The same environment variables the tests themselves read, so a probe can
# never check a different address than the tests use -- which is how the first incident
# (a stale 127.0.0.1:55433 in one file and a live 5432 everywhere else) survived.
# --------------------------------------------------------------------------------------
def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or default).rstrip("/")


def api_url() -> str:
    return _env("MEDOS_E2E_API_URL", "http://127.0.0.1:8000")


def web_url() -> str:
    return _env("MEDOS_E2E_WEB_URL", "http://127.0.0.1:3000")


def gateway_url() -> str:
    return _env("MEDOS_GATEWAY_URL", "http://127.0.0.1:8043")


def tenant_id() -> str:
    """The tenant whose DICOMweb namespace the harness works in.

    The Gateway's public routes carry it (`/dicomweb/{t}/studies`, MOS-DATA-007) and
    MOS-DATA-009 makes a path segment that is not the authenticated principal's tenant a
    403, so this is not decoration. Same default and same variable as
    docker-compose.yml's `MEDOS_TENANT_ID` and `tests/gate/conftest.py`'s `TENANT_A`.
    """
    return os.environ.get("MEDOS_TENANT_ID") or "00000000-0000-0000-0000-000000000000"


def dicomweb_token() -> str:
    """The scoped Gateway token compose hands `medos-worker`.

    MOS-DATA-017: "a Service container MUST receive a scoped Gateway token, never a
    credential". It is NOT a PACS credential -- MOS-DATA-005 keeps that on `medos-gateway`
    alone -- and it grants nothing outside this laptop stack. Same variable and same
    default as docker-compose.yml's `MEDOS_DICOMWEB_TOKEN` and `tests/gate/conftest.py`.
    """
    return os.environ.get("MEDOS_GATEWAY_WORKER_KEY") or "medos-dev-worker-key"


def _gateway_dicomweb_root() -> str:
    """The Gateway's DICOMweb root for this tenant: the harness's only route to DICOM."""
    return f"{gateway_url()}/dicomweb/{tenant_id()}"


def dicomweb_url() -> str:
    """The DICOMweb root the integration suite uses.

    The Gateway, not the PACS. See this module's docstring: the PACS publishes no host
    port and MOS-DATA-006 is why it must not. `MEDOS_DICOMWEB_URL` still overrides.
    """
    return _env("MEDOS_DICOMWEB_URL", _gateway_dicomweb_root())


def e2e_dicomweb_url() -> str:
    """The DICOMweb root the e2e suite uses. Usually, but not always, the same one."""
    return _env("MEDOS_E2E_DICOMWEB_URL", _gateway_dicomweb_root())


def minio_url() -> str:
    return _env("MEDOS_S3_ENDPOINT", "http://127.0.0.1:9000")


def triton_url() -> str:
    return _env("MEDOS_TRITON_HOST_URL", "http://127.0.0.1:8010")


def tritond_url() -> str:
    return _env("MEDOS_TRITOND_URL", "http://127.0.0.1:8500")


def database_url() -> str:
    return (
        os.environ.get("MEDOS_TEST_DATABASE_URL")
        or os.environ.get("MEDOS_DATABASE_URL")
        or "postgresql://medos:medos@127.0.0.1:5432/medos"
    )


# --------------------------------------------------------------------------------------
# Shape-aware HTTP probing
# --------------------------------------------------------------------------------------
_HTTP_TIMEOUT_S = float(os.environ.get("MEDOS_PROBE_TIMEOUT_S", "6"))


def _get(url: str, *, accept: str | None = None, bearer: str | None = None):
    import requests

    headers: dict[str, str] = {}
    if accept:
        headers["Accept"] = accept
    if bearer:
        # MOS-SEC-008 admits anonymous access on /healthz, /readyz and the OpenAPI
        # document and nowhere else, so a DICOMweb probe of the Gateway without this
        # header measures the 401 and not the PACS behind it.
        headers["Authorization"] = f"Bearer {bearer}"
    try:
        return requests.get(url, headers=headers or None, timeout=_HTTP_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - any transport failure is "not reachable"
        raise Unready(f"GET {url} -> {type(exc).__name__}: {exc}") from exc


def _expect(resp, url: str, *, shape: str) -> object:
    """Reject a response that is merely a 2xx.

    `shape` is what the caller needs to be true for the dependency to be usable:
      json        -- a JSON object or array (an HTML SPA fallback fails here)
      dicom-json  -- a QIDO-RS result: a JSON array, or 204 for an empty archive
      javascript  -- a served script, by Content-Type
      html        -- a served document, by Content-Type
      ok          -- a 2xx with no body contract (MinIO's health endpoint is empty)
    """
    if resp.status_code >= 400:
        body = (resp.text or "")[:160].replace("\n", " ")
        raise Unready(f"GET {url} -> HTTP {resp.status_code}: {body}")
    ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    if shape == "ok":
        return None
    if shape == "javascript":
        if "javascript" not in ctype and "ecmascript" not in ctype:
            raise Unready(
                f"GET {url} -> HTTP {resp.status_code} {ctype or '(no content-type)'}: "
                "this origin answers with something that is not the script. An nginx "
                "SPA fallback returns 200 text/html for every unknown path, so a status "
                "code alone would have called this healthy."
            )
        return None
    if shape == "html":
        # A DOCUMENT, AND A NON-EMPTY ONE. The origin's `location /` now answers 404 for
        # everything it does not serve, so a wrong path fails on the status code above --
        # but a bind mount that landed EMPTY would still return 200 with nothing in it,
        # and an empty viewer index is exactly the failure this probe is for.
        if "html" not in ctype:
            raise Unready(
                f"GET {url} -> HTTP {resp.status_code} {ctype or '(no content-type)'}: "
                "expected the viewer's document. This origin serves the clinical surface "
                "from a bind mount; a wrong or empty mount is what this catches."
            )
        if len((resp.text or "").strip()) < 200:
            raise Unready(
                f"GET {url} -> HTTP {resp.status_code}: an HTML body of "
                f"{len(resp.text or '')} bytes. The mount landed empty."
            )
        return None
    if shape == "dicom-json" and (resp.status_code == 204 or not resp.content):
        return []  # PS3.18 8.3.4.3: 204 is an empty archive, not a broken one.
    if "json" not in ctype:
        raise Unready(
            f"GET {url} -> HTTP {resp.status_code} {ctype or '(no content-type)'}: "
            "expected JSON. An origin that answers every path with an HTML page is not "
            "the service this points at."
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise Unready(f"GET {url} -> HTTP {resp.status_code}: unparseable JSON body") from exc
    if shape == "dicom-json" and not isinstance(payload, list):
        raise Unready(
            f"GET {url} -> JSON that is not a QIDO-RS array: {type(payload).__name__}"
        )
    return payload


# --------------------------------------------------------------------------------------
# Orthanc's NATIVE REST API, driven from inside the PACS container.
#
# WHY THIS EXISTS AT ALL, GIVEN THAT THE GATEWAY IS THE ONLY ROUTE TO DICOM
# -------------------------------------------------------------------------
# It exists for exactly one thing the Gateway cannot do. DICOMweb's QIDO/WADO/STOW triad
# has no DELETE verb, so a fixture that stores a study cannot remove it again through
# `medos.dicomweb.DicomWebGateway`; and adding a delete to the Gateway to make a test tidy
# would put a destructive PACS operation into production code that nothing else needs --
# chapter 9's default-DENY table forbids MedicalOS deleting a study or an instance, and
# MOS-EXEC-061 makes recovery past `store_dicom` forward-only BECAUSE nothing can be
# un-stored. There is therefore no delete anywhere in `medos/medos/`, and there must not be.
#
# WHY IT RUNS INSIDE THE CONTAINER
# --------------------------------
# MOS-DATA-006: "A deployment in which any other container can open a TCP connection to
# the PACS port is non-conformant, whether or not it has a credential." Publishing
# 127.0.0.1:8042 so the harness could reach the native API is the defect register entry 70
# is about, and re-publishing it -- or making `pacs` non-internal, or giving `orthanc` a
# second network -- would trade a skip count for a real conformance violation. Executing
# inside `medos-orthanc` and talking to its own loopback is the PACS talking to itself,
# which that sentence does not reach. No host route is created and none is needed.
#
# WHY IT IS ONE HELPER AND NOT AN INLINE `docker exec` AT EACH CALL SITE
# ----------------------------------------------------------------------
# Three fixtures need it: `tests/integration/test_dicomweb.py`, `tests/integration/
# test_worker.py` and `tests/e2e/test_demo.py`. This project has six recorded instances of
# one rule living in two copies that drifted apart; a second teardown path would be the
# seventh, and the two copies would disagree about which failures are silent.
#
# WHY IT TAKES A LIST
# -------------------
# A `docker exec` costs ~3.5 s on a Docker Desktop host. `_purge_derived_series` visits
# every series in the archive, so one exec per HTTP request turned a 3 s teardown into a
# 90 s one. The driver below issues a whole batch inside a single exec; a call site that
# needs a decision between rounds simply makes two calls.
# --------------------------------------------------------------------------------------

#: The container that IS the PACS. Overridable only so a second stack can be driven.
PACS_CONTAINER = os.environ.get("MEDOS_ORTHANC_CONTAINER") or "medos-orthanc"

#: Orthanc's HTTP root as seen from INSIDE that container. Deliberately not a host address:
#: on a conformant deployment there is no host address for it, and that is the point.
PACS_LOOPBACK_URL = "http://localhost:8042"


@dataclass(frozen=True)
class NativeCall:
    """One request to Orthanc's native REST API. `path` is rooted, e.g. `/series`."""

    method: str
    path: str
    body: str | None = None


@dataclass(frozen=True)
class NativeResult:
    """What that request answered. A non-2xx is returned, not raised: several call sites
    legitimately tolerate a 404 (the study another run already removed)."""

    status: int
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        """The decoded body, or None when there was none. Raises `Unready` on garbage."""
        if not self.text.strip():
            return None
        try:
            return json.loads(self.text)
        except ValueError as exc:
            raise Unready(
                f"Orthanc answered HTTP {self.status} with a body that is not JSON: "
                f"{self.text[:160]!r}",
                dependency="orthanc-rest",
            ) from exc


# The program run by `python3` inside the container. `orthancteam/orthanc:25.2.0` ships
# neither curl nor wget (the service's own healthcheck in docker-compose.yml says so and
# for the same reason), so urllib from the standard library is the whole toolkit.
_NATIVE_REST_DRIVER = """
import json, sys, urllib.error, urllib.request

_out = []
for _call in json.loads(_CALLS):
    _body = _call["body"]
    _req = urllib.request.Request(
        _BASE + _call["path"],
        method=_call["method"],
        data=None if _body is None else _body.encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(_req, timeout=_TIMEOUT) as _resp:
            _text = _resp.read().decode("utf-8", "replace")
            _out.append({"status": _resp.status, "text": _text})
    except urllib.error.HTTPError as _exc:
        _out.append({"status": _exc.code, "text": _exc.read().decode("utf-8", "replace")})
json.dump(_out, sys.stdout)
"""

# Stderr fragments that mean "the docker CLI or its daemon is the problem", as opposed to
# "the PACS container is not there". The two need different fixes and therefore different
# dependency keys, which is what `Unready.dependency` carries.
_DOCKER_SIDE_FAILURES = (
    "cannot connect to the docker daemon",
    "is the docker daemon running",
    "error during connect",
    "docker daemon is not running",
)


def _orthanc_exec(calls: Sequence[NativeCall], *, timeout_s: float) -> list[NativeResult]:
    """Issue `calls` against Orthanc from inside `medos-orthanc`. Raises `Unready`.

    Every failure path below raises with a `dependency` set, and none of them swallows: a
    teardown that silently did nothing leaves the next run asserting against an archive
    whose state nobody established, which is the failure
    `tests/e2e/test_demo.py::clean_slate` exists to prevent. The ONLY empty list this
    returns is for an empty `calls` -- a question with no requests in it, not a question
    that went unanswered.
    """
    if not calls:
        # An empty batch is a legitimate call site outcome -- an archive with no series to
        # visit -- and a `docker exec` costs seconds. Answering it here rather than at each
        # call site keeps the guard in one place.
        return []
    docker = shutil.which("docker")
    if docker is None:
        raise Unready(
            "the docker CLI is not on PATH. Orthanc's native REST API is reachable only "
            f"from inside {PACS_CONTAINER} (MOS-DATA-006 forbids a host route to the "
            "PACS), and `docker exec` is how this harness gets inside it.",
            dependency="docker",
        )
    payload = [{"method": c.method, "path": c.path, "body": c.body} for c in calls]
    program = (
        f"_BASE = {PACS_LOOPBACK_URL!r}\n"
        f"_TIMEOUT = {float(timeout_s)!r}\n"
        f"_CALLS = {json.dumps(payload)!r}\n"
    ) + _NATIVE_REST_DRIVER
    try:
        out = subprocess.run(
            [docker, "exec", "-i", PACS_CONTAINER, "python3", "-"],
            input=program,
            capture_output=True,
            text=True,
            timeout=timeout_s * len(payload) + 60.0,
        )
    except subprocess.TimeoutExpired as exc:
        raise Unready(
            f"docker exec {PACS_CONTAINER} python3 did not finish {len(payload)} "
            f"native-REST call(s) within {timeout_s * len(payload) + 60.0:.0f}s",
            dependency="orthanc-rest",
        ) from exc
    except OSError as exc:
        raise Unready(
            f"{docker} could not be run: {type(exc).__name__}: {exc}", dependency="docker"
        ) from exc
    if out.returncode != 0:
        why = (out.stderr.strip() or out.stdout.strip() or "no output")[:400]
        lowered = why.lower()
        dependency = (
            "docker" if any(f in lowered for f in _DOCKER_SIDE_FAILURES) else "orthanc-rest"
        )
        raise Unready(
            f"docker exec {PACS_CONTAINER} python3 exited {out.returncode}: {why}",
            dependency=dependency,
        )
    try:
        rows = json.loads(out.stdout)
    except ValueError as exc:
        raise Unready(
            f"docker exec {PACS_CONTAINER} python3 exited 0 but wrote no parseable "
            f"result: {out.stdout[:200]!r}",
            dependency="orthanc-rest",
        ) from exc
    return [NativeResult(int(row["status"]), str(row["text"])) for row in rows]


def orthanc_native_rest(
    calls: Sequence[NativeCall], *, timeout_s: float = 30.0
) -> list[NativeResult]:
    """`_orthanc_exec`, with an unreachable PACS routed into the skip taxonomy.

    THE ONE entry point for fixture teardown against Orthanc's native REST API. A missing
    docker CLI, a stopped `medos-orthanc` and a non-zero exec each reach `skip_infra` under
    the dependency key whose START_HINT actually fixes them -- never a bare exception, and
    never the silent `except Exception: pass` this replaced. Under `--require-stack` every
    one of them is a FAILURE, which is correct: a teardown that could not run means the
    next assertion has no established left-hand side.

    `tests._support.skips` imports this module, so the import is local; a module-level one
    would be a cycle.
    """
    from tests._support.skips import skip_infra

    try:
        return _orthanc_exec(calls, timeout_s=timeout_s)
    except Unready as exc:
        skip_infra(str(exc), dependency=exc.dependency or "orthanc-rest")


# --------------------------------------------------------------------------------------
# The probes
# --------------------------------------------------------------------------------------
def _probe_postgres() -> str:
    import psycopg

    dsn = database_url()
    where = dsn.rsplit("@", 1)[-1]
    try:
        with psycopg.connect(dsn, connect_timeout=int(_HTTP_TIMEOUT_S) or 5) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        raise Unready(f"{where}: {type(exc).__name__}: {exc}") from exc
    return where


def _probe_orthanc() -> str:
    """QIDO-RS through the Gateway, with the bearer the worker itself presents.

    The probe must dial exactly what the tests dial, which is the rule that would have
    caught the stale `127.0.0.1:55433` in one file and a live 5432 everywhere else. Since
    entry 70 that address is the Gateway, and a Gateway probed without the bearer answers
    401 for a perfectly healthy deployment.
    """
    url = f"{dicomweb_url()}/studies?limit=1"
    _expect(
        _get(url, accept="application/dicom+json", bearer=dicomweb_token()),
        url,
        shape="dicom-json",
    )
    return dicomweb_url()


def _probe_orthanc_e2e() -> str:
    url = f"{e2e_dicomweb_url()}/studies?limit=1"
    _expect(
        _get(url, accept="application/dicom+json", bearer=dicomweb_token()),
        url,
        shape="dicom-json",
    )
    return e2e_dicomweb_url()


def _probe_orthanc_rest() -> str:
    """Orthanc's native REST API, asked from inside the PACS container.

    Rewritten with the mechanism the fixtures now use. The previous version opened a TCP
    connection from the host to 127.0.0.1:8042, which a conformant deployment does not
    answer (MOS-DATA-006) -- so it reported the dependency unreachable on exactly the
    deployments where the fixtures that declare it work. A probe that checks a different
    route than its dependents is worse than no probe: it produces a red run nobody can act
    on, which is how `--require-stack` gets switched off for good.
    """
    result = _orthanc_exec([NativeCall("GET", "/system")], timeout_s=_HTTP_TIMEOUT_S)[0]
    if not result.ok:
        raise Unready(
            f"GET {PACS_LOOPBACK_URL}/system inside {PACS_CONTAINER} -> HTTP "
            f"{result.status}: {result.text[:160]}",
            dependency="orthanc-rest",
        )
    payload = result.json()
    if not isinstance(payload, dict) or "Version" not in payload:
        raise Unready(
            f"{PACS_CONTAINER} answered /system with JSON that has no Version: not "
            "Orthanc's native REST API",
            dependency="orthanc-rest",
        )
    return f"docker exec {PACS_CONTAINER} -> Orthanc {payload['Version']} on its loopback"


def _probe_api() -> str:
    # /readyz and NOT /healthz. Liveness deliberately does not touch the database, so a
    # liveness probe stays green through exactly the outage that took the platform down.
    url = f"{api_url()}/readyz"
    payload = _expect(_get(url), url, shape="json")
    status = (payload or {}).get("status") if isinstance(payload, dict) else None
    if status not in {"ready", "ok"}:
        raise Unready(f"GET {url} -> 200 but status={status!r}: answering, not ready")
    return url


def _probe_gateway() -> str:
    url = f"{gateway_url()}/readyz"
    _expect(_get(url), url, shape="json")
    return url


def _probe_web() -> str:
    """The browser origin, probed BY THE CLINICAL SURFACE IT SERVES.

    This used to fetch `/app-config.js`, which was OHIF's. With OHIF withdrawn that file
    is the extension's deployment config and says nothing about whether the viewer a
    clinician opens is being served at all -- and the scoping survey found that
    `/mos-viewer/` appeared in NO test and NO CI step, so nothing else said it either.
    Probing the viewer's own index proves the bind mount landed and the surface answers.
    """
    url = f"{web_url()}/mos-viewer/index.html"
    _expect(_get(url), url, shape="html")
    return url


def _probe_minio() -> str:
    url = f"{minio_url()}/minio/health/ready"
    _expect(_get(url), url, shape="ok")
    return url


def _probe_triton() -> str:
    url = f"{triton_url()}/v2/health/ready"
    _expect(_get(url), url, shape="ok")
    return url


def _probe_tritond() -> str:
    url = f"{tritond_url()}/healthz"
    _expect(_get(url), url, shape="json")
    return url


def _probe_worker() -> str:
    """The runner has no HTTP surface, so its own compose healthcheck is the signal.

    "cannot tell" is NOT "healthy": with no docker CLI this raises, because a probe that
    cannot see its dependency must not report it up. That is the whole lesson of the two
    incidents.
    """
    return _docker_health("medos-worker")


def _probe_sealed_service() -> str:
    """Z-SERVICE has no host-reachable surface, and that is the point, not a gap.

    Every other probe in this file opens a socket from the HOST. This one cannot, and must
    not be made able to: the host is `Z-EDGE`, chapter 8's zone table grants a sealed
    service ingress from `Z-PLATFORM` only, and `medos-sealed-service` therefore joins the
    `internal: true` `sealed` network and publishes no port. A probe that reached it from
    here would be evidence that the isolation this container exists to demonstrate is
    broken.

    So the signal is the container's own compose healthcheck, which runs INSIDE the
    network boundary and checks `/healthz` -- row 1 of the section 2.5.2 ABI table. Same
    construction, and same reason, as `_probe_worker` above.
    """
    return _docker_health("medos-sealed-service")


def _probe_docker() -> str:
    """The docker CLI, and a daemon that answers it.

    Both halves, because either one alone is a false positive. A CLI on PATH with Docker
    Desktop stopped answers `docker inspect` with a transport error, which is the failure
    a suite would otherwise meet in the middle of a run; `docker version` asks the SERVER
    for its version, so a dead daemon cannot satisfy it.

    "I could not tell" is never "it is up" -- the rule the whole file is written from.
    """
    path = shutil.which("docker")
    if path is None:
        raise Unready(
            "the docker CLI is not on PATH. Suites that read a container's environment "
            "or its healthcheck have no other way to ask."
        )
    out = subprocess.run(
        [path, "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        why = (out.stderr.strip() or out.stdout.strip() or "no output")[:200]
        raise Unready(f"{path} is on PATH but the daemon did not answer: {why}")
    return f"docker server {out.stdout.strip() or '(unknown version)'}"


def _docker_health(container: str) -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise Unready(
            f"{container} has no network surface to probe and the docker CLI is not on "
            "PATH, so its state cannot be established. Refusing to assume it is up."
        )
    fmt = "{{.State.Status}}:{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
    out = subprocess.run(
        [docker, "inspect", "-f", fmt, container],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        why = out.stderr.strip() or "no such container"
        raise Unready(f"docker inspect {container}: {why}")
    state, _, health = out.stdout.strip().partition(":")
    if state != "running":
        raise Unready(f"container {container} is {state!r}, not running")
    if health not in {"healthy", "none"}:
        raise Unready(f"container {container} is running but its healthcheck says {health!r}")
    return f"docker://{container} ({state}, health={health})"


@dataclass(frozen=True)
class Dep:
    key: str
    what: str
    check: Callable[[], str]


PROBES: dict[str, Dep] = {
    dep.key: dep
    for dep in (
        Dep("postgres", "the platform database", _probe_postgres),
        Dep(
            "orthanc",
            "the PACS, over DICOMweb through the Gateway (integration suite's root)",
            _probe_orthanc,
        ),
        Dep(
            "orthanc-e2e",
            "the PACS, over DICOMweb through the Gateway (e2e suite's root)",
            _probe_orthanc_e2e,
        ),
        Dep(
            "orthanc-rest",
            "the PACS, over its native REST API from inside its own container",
            _probe_orthanc_rest,
        ),
        Dep("medos-api", "the HTTP surface, readiness", _probe_api),
        Dep(
            "medos-gateway",
            "the worker's and the viewer's only route to DICOM",
            _probe_gateway,
        ),
        Dep("medos-worker", "the claim loop and the eight-step executor", _probe_worker),
        Dep("web", "the browser origin and its nginx", _probe_web),
        Dep("minio", "the object store that holds the model repository", _probe_minio),
        Dep("triton", "the shared inference server", _probe_triton),
        Dep("medos-tritond", "the residency manager in front of Triton", _probe_tritond),
        Dep(
            "medos-sealed-service",
            "the reference sealed-mode vendor container (Z-SERVICE)",
            _probe_sealed_service,
        ),
        # NOT a compose service; see the module docstring's last section.
        Dep("docker", "the docker CLI and a daemon that answers it", _probe_docker),
    )
}

# Compose services with no probe, and why. `tests/unit/test_stack_contract.py` fails if a
# service is in neither this table nor PROBES, so "nobody classified it" stops being
# possible.
UNCOVERED: dict[str, str] = {
    "medos-model-publish": (
        "a one-shot init job (`restart: no`): it publishes the self-test model into MinIO "
        "and exits. A stopped medos-model-publish is the SUCCESS state, so there is "
        "nothing to probe; what it produces is covered by the `minio` probe."
    ),
    "medos-trainer-environment": (
        "a one-shot init job, on the same argument as medos-model-publish: it writes "
        "MEDOS_TRAINING_ENVIRONMENT's nine keys into a volume and exits, so a STOPPED "
        "container is the success state. What it produces is probed by proxy -- "
        "`medos-api` depends on it with `service_completed_successfully`, so a healthy "
        "API means this job exited 0, and `tests/integration/test_trainer_image.py` "
        "asserts the document it writes is one the router can read."
    ),
    "medos-trainer": (
        "behind `profiles: [training]`, so it is ABSENT from a default `up` and a probe "
        "for it would fail on every correct deployment that is not currently training. "
        "It occupies the GPU for hours per run, which is why it is opt-in; "
        "`tests/integration/test_trainer_image.py` exercises the same image directly "
        "with `docker run`, which is the honest way to test a service nobody is "
        "required to be running."
    ),
    "medos-train-api": (
        "behind `profiles: [training]`, so it is ABSENT from a default `up` and a probe "
        "for it would fail on every correct deployment of the PACS-and-models service. "
        "It serves a strict SUPERSET of `medos-api`'s routes, so nothing it offers is "
        "unprobed -- what `medos-api`'s probe establishes about the shared 25 paths is "
        "established about this process too, and the 27 it adds are covered by "
        "`tests/integration/test_api_training.py` and `test_api_curation*.py`, which "
        "drive the routers directly. `tests/gate/test_core_train_boundary.py` asserts "
        "the superset relation itself, so the two cannot drift into a fork while this "
        "reasoning stays true."
    ),
    "medos-seed-corpus": (
        "BOTH reasons at once: a one-shot (`restart: no`) whose stopped container is the "
        "success state, AND behind `profiles: [demo]`, so it is absent from a default "
        "`up` -- a probe would fail on every correct deployment that has not opted into "
        "seeding. What it produces is not probed by proxy either, deliberately: the "
        "suite must NOT depend on a seeded archive, because a test that passes only "
        "after somebody ran the demo is a test that fails for a stranger. "
        "`tests/unit/test_demo_corpus.py` exercises the generator directly -- the "
        "envelope arithmetic, the UID root, the idempotence and the refusal outside "
        "`MEDOS_ENV=dev` -- none of which needs a container."
    ),
}

# The compose service name each probe key stands for, where they differ. Only used by the
# coverage test.
PROBE_SERVICE: dict[str, str] = {
    "orthanc": "orthanc",
    "orthanc-e2e": "orthanc",
    "orthanc-rest": "orthanc",
    "medos-tritond": "medos-tritond",
}


# --------------------------------------------------------------------------------------
# Which dependencies each part of the suite declares.
#
# Longest matching path prefix wins, so a module can be more precise than its directory.
# Keeping this at MODULE granularity is what stops a correct run going red for a
# dependency the selected tests never touch -- a false red is how a switch like
# --require-stack gets turned off for good.
# --------------------------------------------------------------------------------------
SUITE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    # Unit tests must not touch a container. Declaring nothing is the assertion.
    "tests/unit": (),
    # The nnU-Net internals gate asserts facts about an INSTALLED PYTHON PACKAGE, not about
    # a running service, so it declares nothing -- and the empty tuple is the assertion, as
    # it is for tests/unit.
    #
    # It needs the `nnunetv2` and `torch` pins, which are trainer-image dependencies the
    # platform deliberately does not carry (MOS-TRAIN-225). That is NOT expressible here:
    # PROBES keys are services to connect to, and a missing import is not an unready
    # container. It is handled where it belongs -- the module imports nnunetv2 at top level
    # and FAILS when the pin is absent, because a gate that skips when its dependency is
    # missing is not a gate. The suite is kept out of the default run by selection
    # (`-m trainer_internals`, its own CI job), never by skipping.
    "trainer/tests": (),
    # The default for the integration suite: Postgres only.
    "tests/integration": ("postgres",),
    # `orthanc-rest` and `docker` because `_delete_study`'s teardown now runs
    # `docker exec medos-orthanc python3` -- DICOMweb has no DELETE verb. A dependency a
    # suite genuinely has belongs in the PREFLIGHT, not in a skip taken part-way through a
    # run whose earlier assertions already passed; that is the same rule this row's
    # neighbours follow, and it was missed here when the teardown moved.
    "tests/integration/test_dicomweb.py": ("postgres", "orthanc", "orthanc-rest", "docker"),
    # The Gateway suite drives the medos-gateway CONTAINER on 127.0.0.1:8043 as well as an
    # in-process app. `orthanc` is deliberately NOT declared, and the reason survives entry
    # 70 unchanged: this suite's subject IS the Gateway, so a second key standing for the
    # PACS behind it would probe the same origin twice under two names. `medos-gateway`
    # covers it.
    "tests/integration/test_gateway.py": ("postgres", "medos-gateway"),
    # The sealed-mode suite. `postgres` is NOT declared and the omission is deliberate:
    # nothing in it touches the database, and chapter 2 section 2.7 (MOS-SVC-063) is the
    # reason -- a sealed execution reaches no datastore, so a suite that proved sealed
    # isolation would be declaring a dependency the thing it tests must not have.
    #
    # `medos-sealed-service` is the vendor container; `medos-gateway` and `medos-worker`
    # are the two containers the zone table lets it talk to, and the isolation test's
    # CONTROL probes run from them. A negative from a probe that cannot work is
    # indistinguishable from isolation, so their absence must abort the run rather than
    # let it pass.
    "tests/integration/test_sealed_mode.py": (
        "medos-sealed-service",
        "medos-gateway",
        "medos-worker",
    ),
    # `docker` is declared because `orthanc-rest` is. Since entry 70 the native REST API is
    # reached by executing inside `medos-orthanc` -- MOS-DATA-006 leaves no host route to
    # it -- so the docker CLI is a dependency this suite genuinely has. Declared here it is
    # a preflight abort that names it; undeclared it was a skip from inside the first
    # fixture that shelled out, part-way through a run.
    "tests/integration/test_worker.py": ("postgres", "orthanc", "orthanc-rest", "docker"),
    # The e2e suite's claim is "the medos/deploy/compose stack works end to end", so its
    # declaration is the deployment. medos-gateway is in it because the worker reaches
    # DICOM through it and the viewer proxies /dicomweb/ to it; triton and the tritond
    # because the worker container is configured with MEDOS_TRITON_URL and a dead
    # inference plane is a dead platform, not a slow one.
    # `docker` for the same reason as the test_worker.py row above: `clean_slate` resets
    # the archive through Orthanc's native REST API, from inside the PACS container.
    "tests/e2e": (
        "postgres",
        "orthanc-e2e",
        "orthanc-rest",
        "docker",
        "medos-gateway",
        "medos-api",
        "medos-worker",
        "web",
        "minio",
        "triton",
        "medos-tritond",
    ),
    # MOS-SAFE-089a's machine-checkable half: is the MedicalOS extension in the bytes the
    # viewer serves? It fetches static assets from the viewer origin and nothing else -- no
    # job, no DICOM, no database -- so declaring the whole e2e set above would make it
    # unrunnable on a correctly isolated deployment, where the host has no route to the
    # PACS at all (MOS-DATA-006). Module granularity exists for exactly this case.
    "tests/e2e/test_viewer_extension.py": ("web",),
    # The release-0.1.0 gate (docs/spec/15-delivery.md 15.1.2). Its claim is that the
    # DEPLOYMENT satisfies the five named checks, so its declaration is the deployment --
    # with two deliberate differences from the e2e row above.
    #
    # NO `orthanc*` KEY, and this row is the one that was RIGHT all along. It has always
    # reached DICOM through medos-gateway with a bearer, so it was the only row entry 70's
    # defect never touched: while the three orthanc probes dialled a host port that a
    # conformant deployment cannot publish, this gate kept running. The `orthanc` and
    # `orthanc-e2e` probes now dial the same Gateway root this row's fixtures do, so
    # declaring one here would probe the same origin twice under two names; `orthanc-rest`
    # is fixture teardown, and no gate check tears down an archive.
    #
    # NO `web`. The viewer is Tier B in 15.1.3 and none of the five checks touches it;
    # a gate that goes red for a dependency it never uses is a gate people learn to
    # bypass. triton/minio/medos-tritond ARE declared: the worker container is configured
    # with MEDOS_TRITON_URL and a dead inference plane is a dead platform, not a slow one.
    "tests/gate": (
        "postgres",
        "medos-gateway",
        "medos-api",
        "medos-worker",
        "minio",
        "triton",
        "medos-tritond",
    ),
    # THE RELEASE-0.2.0 GATE, per module. The row above is the 0.1.0 row's declaration and
    # it is the whole deployment, because those five checks are claims about what the
    # running images do. The 0.2.0 checks are not: MOS-EVID-006 puts the evidence plane
    # outside the serving path entirely, so there is no API to call, no queue to enqueue on
    # and no worker to wait for. Declaring the deployment for them would make
    # `-m gate_0_2_0 --require-stack` unrunnable on a machine that has only Postgres -- a
    # false red, which is how a switch like --require-stack gets turned off for good.
    #
    # Module granularity exists for exactly this, and these six rows are what stop the
    # directory default above from being inherited.
    "tests/gate/test_deployment_gate.py": ("postgres",),
    "tests/gate/test_non_inferiority.py": ("postgres",),
    "tests/gate/test_per_case_metrics.py": ("postgres",),
    "tests/gate/test_leakage_check.py": ("postgres",),
    # These two declare NOTHING, and the empty tuple is the assertion. `ruo-marking` writes
    # four DICOM files to a temporary directory and calls the writer; `report-offline-verify`
    # builds a tar.gz and verifies it in a subprocess that cannot open a socket. Neither
    # touches a container, and a gate check whose claim is "this works with no MedicalOS in
    # sight" must not need MedicalOS running to be believed.
    "tests/gate/test_ruo_marking.py": (),
    "tests/gate/test_report_offline_verify.py": (),
    # THE RELEASE-0.3.0 ROW'S ONE MODULE THAT NEEDS MORE THAN THE DIRECTORY DEFAULT.
    # `capability-reachable` (register entry 68) asks whether a capability this deployment
    # serves is admitted, resolved, published against, executed AND offered to a reader,
    # and the last two of those reach outside the `tests/gate` row above.
    #
    # `docker`, because the whole check is anchored on `docker inspect`: the served set it
    # iterates is `MEDOS_CAPABILITY_PROVIDERS` as the RUNNING containers hold it, not as
    # docker-compose.yml declares it. A missing CLI used to surface as a skip from inside
    # the first fixture that shelled out, part-way through a gate run; declared here it is
    # a preflight abort that names the dependency and the command that fixes it.
    #
    # `web`, because property 5 fetches `/app-config.js` from the browser origin and reads
    # `window.MEDICALOS.capabilities` out of the bytes the CONTAINER serves.
    # MOS-SAFE-089a makes that array the only job-creation path a reader has, so a
    # capability missing from it is unreachable however healthy the platform is -- that is
    # defect (b) of entry 68, and it is the half of the check that this tree's copy of the
    # config file cannot answer. The `tests/gate` row above still declares no `web` and
    # that stays correct: no other check in any row opens the viewer.
    #
    # NOTE that longest-prefix-wins means this row REPLACES the directory row rather than
    # adding to it, so everything that row declares is repeated here.
    "tests/gate/test_capability_reachable.py": (
        "docker",
        "postgres",
        "medos-gateway",
        "medos-api",
        "medos-worker",
        "web",
        "minio",
        "triton",
        "medos-tritond",
    ),
}


def _normalise(path: str) -> str:
    return str(path).replace("\\", "/")


def dependencies_for_paths(paths: Iterable[str]) -> dict[str, list[str]]:
    """Map dependency key -> the selected paths that declare it.

    Longest prefix wins. A path under no declared prefix contributes nothing, which is
    correct: a suite that has not declared a dependency cannot have one probed for it.
    """
    prefixes = sorted(SUITE_DEPENDENCIES, key=len, reverse=True)
    out: dict[str, list[str]] = {}
    for raw in paths:
        path = _normalise(raw)
        try:
            rel = _normalise(Path(path).resolve().relative_to(REPO_ROOT))
        except (ValueError, OSError):
            rel = path
        for prefix in prefixes:
            if rel == prefix or rel.startswith(prefix + "/"):
                for key in SUITE_DEPENDENCIES[prefix]:
                    out.setdefault(key, []).append(rel)
                break
    return out


def probe(key: str) -> tuple[bool, str]:
    """Run one probe. Returns (ready, detail)."""
    dep = PROBES.get(key)
    if dep is None:  # pragma: no cover - guarded by test_stack_contract.py
        return False, f"no probe is defined for {key!r}"
    try:
        return True, dep.check()
    except Unready as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - a probe that crashes is a probe that failed
        return False, f"{type(exc).__name__}: {exc}"


_SERVICE_RE = re.compile(r"^  ([a-z][a-z0-9_.-]*):\s*$")


def compose_service_names(compose_file: Path | None = None) -> list[str]:
    """Service names from docker-compose.yml, by indentation.

    A two-space-indented key inside the `services:` block. No YAML parser, because
    PyYAML is not a dependency of this project and adding one so that a test can read a
    list of names would be the tail wagging the dog.
    """
    text = (compose_file or COMPOSE_FILE).read_text(encoding="utf-8")
    names: list[str] = []
    in_services = False
    for line in text.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
            continue
        if in_services and line and not line.startswith(" ") and not line.startswith("#"):
            break  # a new top-level key: volumes:, networks:, ...
        match = _SERVICE_RE.match(line)
        if in_services and match:
            names.append(match.group(1))
    return names
