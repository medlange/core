# SPDX-License-Identifier: Apache-2.0
"""`sealed-mode-isolation` -- §15.1.2's release-0.3.0 gate row, fourth check.

    "a sealed service container has no route to the database, the broker or the object
     store -- asserted by network policy test"

and chapter 8's acceptance check 12 is the same sentence with the ports named:

    "From a running `sealed` service container, TCP connections to the PACS port, the
     PostgreSQL port, the Triton port and the object-store port all fail to connect."

THE SUBJECT IS THE RUNNING DEPLOYMENT AND IT CANNOT BE ANYTHING ELSE
----------------------------------------------------------------------
`MOS-SEC-004` is explicit that this "MUST be enforced by NetworkPolicy (Kubernetes) or by
per-service networks (compose), **not only by configuration of the client**". A test that
imported the vendor's code and observed that it never opens a socket would be checking the
client. So this module opens the sockets itself, from INSIDE `medos-sealed-service`, with
`docker exec` and a raw `socket.create_connection`.

To IP ADDRESSES, never to DNS names. A name that does not resolve proves the container is
outside that network's DNS domain and proves nothing about the route; a deployment that put
Postgres on a shared network while leaving the alias off would pass a name-only check while
being wide open.

TWO HALVES, AND THE SECOND ONE IS WHY THE FIRST KEEPS WORKING
----------------------------------------------------------------
The LIVE probe says "this container cannot reach Postgres now". The STRUCTURAL half --
`medos.sealed.isolation.isolation_violations` over `docker inspect` -- says "and nothing
added to the `medos` network tomorrow will be reachable either, because the sealed
container is not on it". `MOS-SVC-064`'s broker is the live case for that: it is `JobQueue`
driver 2, it lands in THIS release, and the route to it must already be absent rather than
removed afterwards.

EVERY ASSERTION HERE IS A NEGATIVE, SO THE CONTROLS ARE LOAD-BEARING
----------------------------------------------------------------------
A negative from a probe that cannot work is indistinguishable from isolation, and a gate
that cannot tell those apart is worse than no gate. Two positives therefore run alongside
and this check does not report a pass without them:

  1. the sealed container CAN open a connection to the Gateway -- its one permitted egress
     (`MOS-SVC-025`, `MOS-SEC-029`) -- and on exactly ONE of the Gateway's interfaces;
  2. `medos-worker` CAN reach Postgres with the SAME probe, so the technique works on this
     machine.

The probe pattern, the `_UNPROBEABLE` taxonomy and the insistence on a control are the ones
`tests/integration/test_sealed_mode.py` established for `MOS-SEC-004` and
`tests/integration/test_gateway.py` before it for `MOS-DATA-006`. They are re-implemented
here rather than imported, for the reason `tests/gate/conftest.py` gives about the 0.1.0
row: a gate module that reaches into the integration suite inherits that suite's fixtures,
its skips and its future edits, and the gate's subject is the deployment.

Needs docker and the running `medos/deploy/compose` stack.

Spec: chapter 8 §8.1.1, §8.6 and acceptance check 12; chapter 2 §2.7 and acceptance check
10; MOS-SEC-004, MOS-SEC-005, MOS-SEC-025, MOS-SEC-029, MOS-SEC-087, MOS-SEC-088,
MOS-SEC-094, MOS-SEC-096, MOS-SVC-025, MOS-SVC-050, MOS-SVC-062..MOS-SVC-065, MOS-REL-004.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from medos.sealed import isolation as iso

from tests._support.skips import skip_environment, skip_infra

pytestmark = pytest.mark.gate_0_3_0

SEALED_CONTAINER = iso.SEALED_SERVICE_CONTAINER
GATEWAY_CONTAINER = "medos-gateway"

#: How many destination/port pairs a probe run must cover before its silence means
#: anything. Six is four destinations' worth: a run that only reached the database and
#: the PACS has not been told about the object store or the inference server.
_PROBE_FLOOR = 6
INVOKER_CONTAINER = "medos-worker"
GATEWAY_PORT = 8043
POSTGRES_PORT = 5432

#: Output that means "this container could not be ASKED", as opposed to "this container
#: was asked and could not connect". The difference is the whole check.
_UNPROBEABLE = (
    "no such container",
    "is not running",
    "is restarting",
    "executable file not found",
    "oci runtime exec failed",
    "cannot exec",
)


def _docker() -> str:
    path = shutil.which("docker")
    if path is None:
        skip_infra(
            "the docker CLI is not on PATH, so Z-SERVICE's network isolation cannot be "
            "established. MOS-SEC-004 requires enforcement by per-service networks, and "
            "this gate refuses to assume it holds.",
            dependency="docker",
        )
    return path


def _docker_out(*args: str, timeout: int = 90) -> tuple[int, str]:
    proc = subprocess.run(
        [_docker(), *args], capture_output=True, text=True, timeout=timeout
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _tcp_probe_from(container: str, address: str, port: int) -> str | None:
    """`reached`, `refused`, or None when the container cannot be asked at all.

    A raw socket and not a database driver: the sealed image ships no psycopg, no boto3
    and no Kafka client, and reading that absence as the boundary is the mistake this
    probe exists to refuse. A raw socket is what an attacker inside the container has.
    """
    python = (
        "import socket\n"
        f"socket.create_connection(('{address}', {port}), 4).close()\n"
        "print('MEDOS_REACHED')\n"
    )
    attempts = (
        ["exec", container, "python", "-c", python],
        ["exec", container, "python3", "-c", python],
        [
            "exec", container, "sh", "-c",
            f"nc -w 4 -z {address} {port} && echo MEDOS_REACHED || echo MEDOS_REFUSED",
        ],
    )
    for args in attempts:
        code, out = _docker_out(*args)
        if "MEDOS_REACHED" in out:
            return "reached"
        if any(marker in out.lower() for marker in _UNPROBEABLE):
            continue  # the tool or the container is missing; this says nothing
        if "MEDOS_REFUSED" in out or code != 0:
            return "refused"
    return None


def _observe(container: str) -> iso.ContainerObservation:
    """One `docker inspect`, reduced to the fields `medos/medos/sealed/isolation.py` reads.

    `{{json .}}` and not a field template: docker's Go templates have no `dict` function,
    and a template that fails to parse exits non-zero in a way that reads exactly like
    "the container is not running" -- which would report a MISSING observation as an
    isolated one.
    """
    code, out = _docker_out("inspect", container, "--format", "{{json .}}", timeout=60)
    if code != 0:
        return iso.ContainerObservation(name=container, running=False)
    raw = json.loads(out.strip())
    settings = raw.get("NetworkSettings") or {}
    host = raw.get("HostConfig") or {}
    config = raw.get("Config") or {}
    return iso.ContainerObservation(
        name=container,
        networks={
            key: (value or {}).get("IPAddress", "")
            for key, value in (settings.get("Networks") or {}).items()
        },
        published_ports=tuple(
            port for port, bindings in (settings.get("Ports") or {}).items() if bindings
        ),
        user=config.get("User") or "",
        read_only_root_filesystem=bool(host.get("ReadonlyRootfs")),
        privileged=bool(host.get("Privileged")),
        cap_add=tuple(host.get("CapAdd") or ()),
        cap_drop=tuple(host.get("CapDrop") or ()),
        env_names=tuple(line.split("=", 1)[0] for line in (config.get("Env") or ())),
        running=(raw.get("State") or {}).get("Status") == "running",
    )


def _absent(name: str) -> iso.ContainerObservation:
    return iso.ContainerObservation(name=name, running=False)


@pytest.fixture(scope="module")
def sealed() -> iso.ContainerObservation:
    observation = _observe(SEALED_CONTAINER)
    if not observation.running:
        skip_infra(
            f"{SEALED_CONTAINER} is not running, so nothing in this check would be "
            f"evidence of isolation. `docker compose up -d medos-sealed-service` in "
            f"medos/deploy/compose.",
            dependency="medos-sealed-service",
        )
    return observation


@pytest.fixture(scope="module")
def deployment() -> dict[str, iso.ContainerObservation]:
    names = {SEALED_CONTAINER, GATEWAY_CONTAINER, INVOKER_CONTAINER}
    names.update(c for dest in iso.FORBIDDEN_DESTINATIONS for c in dest.containers)
    return {name: _observe(name) for name in sorted(names)}


# =====================================================================================
# The controls run FIRST. Nothing below them means anything without them.
# =====================================================================================
def test_sealed_mode_isolation_the_controls_prove_the_negatives_mean_something(
    deployment: dict[str, iso.ContainerObservation],
) -> None:
    """Two positives, and this check reports nothing without them. See the docstring.

    Ordered first in the module so that a reader of a green run sees the controls passed
    before the negatives that depend on them.
    """
    gateway = deployment.get(GATEWAY_CONTAINER) or _absent(GATEWAY_CONTAINER)
    if not gateway.running:
        skip_infra(
            f"{GATEWAY_CONTAINER} is not running, so the sealed container's ONE permitted "
            f"egress cannot be exercised and every refusal below is unattributable",
            dependency="medos-gateway",
        )
    verdicts = {
        address: _tcp_probe_from(SEALED_CONTAINER, address, GATEWAY_PORT)
        for address in sorted(set(gateway.networks.values()))
        if address
    }
    if "reached" not in verdicts.values():
        skip_infra(
            f"the sealed container could not reach the Gateway on any address "
            f"({verdicts}). Either medos-gateway is restarting or the deployment's one "
            f"permitted egress is broken; in both cases the isolation this check asserts "
            f"is unproven rather than proven.",
            dependency="medos-gateway",
        )
    # And on exactly ONE interface. The Gateway is on several networks and Z-SERVICE may
    # meet it on one of them -- a sharper statement of MOS-SEC-029 than "reachable".
    assert sum(1 for v in verdicts.values() if v == "reached") == 1, (
        f"the sealed container reached the Gateway on {verdicts}. MOS-SEC-029 grants "
        f"egress to the Gateway ADDRESS, not to every interface the Gateway holds; a "
        f"second shared interface is a second route into whatever else is on it."
    )

    postgres = deployment.get("medos-postgres") or _absent("medos-postgres")
    if not postgres.running:
        skip_infra("medos-postgres is not running", dependency="postgres")
    worker = {
        address: _tcp_probe_from(INVOKER_CONTAINER, address, POSTGRES_PORT)
        for address in sorted(set(postgres.networks.values()))
        if address
    }
    if "reached" not in worker.values():
        skip_infra(
            f"medos-worker could not open a connection to Postgres ({worker}) with the "
            f"same probe that reports the sealed container isolated. The probe technique "
            f"does not work on this machine, so nothing below is established.",
            dependency="postgres",
        )


# =====================================================================================
# THE CHECK -- chapter 8 acceptance check 12, executed
# =====================================================================================
def test_sealed_mode_isolation_no_tcp_connection_to_any_forbidden_destination(
    sealed: iso.ContainerObservation,
    deployment: dict[str, iso.ContainerObservation],
) -> None:
    """Every forbidden destination, every IP it holds, every port it listens on.

    The database, the object store, the event bus, the shared inference server, the PACS
    and the residency manager -- `medos/medos/sealed/isolation.py` holds the list with the
    requirement id and the reason beside each, so that this test, an operator's pre-flight
    and a future NetworkPolicy generator read one list.
    """
    probed: dict[str, str] = {}
    unreachable_to_ask: list[str] = []
    for destination in iso.FORBIDDEN_DESTINATIONS:
        for container in destination.containers:
            observation = deployment.get(container) or _absent(container)
            if not observation.running:
                unreachable_to_ask.append(container)
                continue
            for address in sorted(set(observation.networks.values())):
                if not address:
                    continue
                for port in destination.ports:
                    verdict = _tcp_probe_from(SEALED_CONTAINER, address, port)
                    if verdict is None:
                        continue
                    probed[f"{destination.zone} {container} {address}:{port}"] = verdict

    if not probed:
        skip_environment(
            "no forbidden destination could be probed from the sealed container, so "
            "MOS-SEC-004 cannot be established from inside the deployment",
            detail="no-probeable-destination",
        )

    reached = sorted(key for key, verdict in probed.items() if verdict == "reached")
    assert not reached, (
        "chapter 8 acceptance check 12 violated -- the sealed container opened a TCP "
        f"connection to {reached}. Z-SERVICE has egress to Z-GATEWAY and the platform "
        "callback only (MOS-SEC-029). Check the `networks:` membership of "
        "medos-sealed-service in medos/deploy/compose/docker-compose.yml."
    )
    # A THIN RESULT IS NOT THE SAME FAILURE AS A CROSSED BOUNDARY, and this assertion
    # used to report both with the same words. On a machine where MinIO, Triton and the
    # broker are simply not up, three pairs get probed, none is reached, and the suite
    # said "too thin a result to call the zone boundary established" -- which reads as a
    # verdict on the deployment rather than on the machine. The floor keeps its teeth
    # where it has them: a shortfall the absent containers do not account for is still a
    # failure, because that is a stack that IS up and still cannot be probed.
    if len(probed) < _PROBE_FLOOR and unreachable_to_ask:
        skip_environment(
            f"only {len(probed)} of the {_PROBE_FLOOR} destination/port pairs this check "
            f"wants could be probed, and {sorted(set(unreachable_to_ask))} are not "
            "running on this machine. Nothing that WAS probed was reached",
            detail="forbidden-destination-not-deployed",
        )
    assert len(probed) >= _PROBE_FLOOR, (
        f"only {len(probed)} destination/port pairs were probed ({sorted(probed)}); that "
        f"is too thin a result to call the zone boundary established. Containers that "
        f"could not be asked: {sorted(set(unreachable_to_ask))}"
    )
    assert sealed.running


def test_sealed_mode_isolation_the_membership_holds_for_destinations_not_yet_deployed(
    sealed: iso.ContainerObservation,
    deployment: dict[str, iso.ContainerObservation],
) -> None:
    """The STRUCTURAL half, and it is the half that covers the broker.

    `MOS-SVC-064` forbids a service the event bus. The broker is `JobQueue` driver 2 and
    lands in THIS release; a probe cannot refute a route to a container that is not up.
    Membership can: whatever name the broker arrives under, it joins `medos` with the rest
    of Z-DATA, and Z-SERVICE is not on `medos`.
    """
    problems = iso.isolation_violations(sealed, deployment)
    assert not problems, (
        "Z-SERVICE's network membership is wrong:\n  " + "\n  ".join(problems)
    )
    assert sealed.network_keys() == {iso.SEALED_NETWORK_SUFFIX}, (
        f"the sealed container is on {sorted(sealed.network_keys())}; the zone table "
        f"grants it one network"
    )
    broker = next(d for d in iso.FORBIDDEN_DESTINATIONS if d.what == "the event bus")
    for name in broker.containers:
        observed = deployment.get(name) or _absent(name)
        if not observed.running:
            continue
        shared = observed.network_keys() & sealed.network_keys()
        assert not shared, f"{name} shares network(s) {sorted(shared)} with Z-SERVICE"


def test_sealed_mode_isolation_the_container_is_hardened_and_holds_no_credential(
    sealed: iso.ContainerObservation,
) -> None:
    """`MOS-SVC-050` and `MOS-SEC-025`/`MOS-SEC-087`, read off the RUNNING container.

    Off the running container and not off `docker-compose.yml`, because the running
    container is what an attacker meets. `MOS-SEC-025` names container inspection output
    as the place a Job Token would leak to, so the environment is read from exactly there.
    """
    problems = iso.hardening_violations(sealed)
    assert not problems, "MOS-SVC-050 violated:\n  " + "\n  ".join(problems)

    credentials = [
        name
        for name in sealed.env_names
        if any(fragment in name.upper() for fragment, _ in iso.FORBIDDEN_ENV_SUBSTRINGS)
    ]
    assert not credentials, (
        f"the sealed container holds {credentials} in its environment. Every other "
        f"service in the deployment carries a DSN, a bucket or a token; Z-SERVICE carries "
        f"none (MOS-SEC-087), and MOS-SEC-025 forbids the Job Token here specifically."
    )


def test_sealed_mode_isolation_the_membership_checker_reports_a_broken_deployment(
    deployment: dict[str, iso.ContainerObservation],
) -> None:
    """The structural half's own negative control.

    `isolation_violations` returning `[]` for a correct deployment is only meaningful if
    it returns something for a wrong one. Fed a sealed container that has been added to
    the `medos` network, it must say so -- otherwise the green above is a function that
    always returns the empty list.
    """
    broken = iso.ContainerObservation(
        name=SEALED_CONTAINER,
        networks={"compose_sealed": "172.30.0.9", "compose_medos": "172.31.0.9"},
        user="65534:65534",
        read_only_root_filesystem=True,
        cap_drop=("ALL",),
        running=True,
    )
    problems = iso.isolation_violations(broken, deployment)
    assert problems, (
        "isolation_violations() reported nothing for a sealed container placed on the "
        "`medos` network. It is not checking membership, and the pass above is an "
        "artifact of the function rather than a property of the deployment."
    )


def test_sealed_mode_isolation_there_is_no_route_to_the_internet(
    sealed: iso.ContainerObservation,
) -> None:
    """Chapter 2 acceptance check 10, first half. Threat T3: exfiltration of a study.

    `MOS-SVC-025`: "`network.egress` MUST be a subset of `["dicom-gateway"]`. There is no
    manifest syntax for an arbitrary host." The vendor's container is the one place in
    this deployment where the code is not ours.

    THE CONTROL MATTERS MORE HERE THAN ANYWHERE. An air-gapped CI runner passes this test
    for a reason that has nothing to do with the deployment, and goes on passing it the
    day `internal: true` is deleted. So the same probe runs from `medos-api`.
    """
    control = _tcp_probe_from("medos-api", "1.1.1.1", 443)
    if control != "reached":
        skip_environment(
            "medos-api cannot open an outbound connection either, so this machine cannot "
            "tell egress deny-all from having no uplink",
            detail="no-outbound-route-from-this-host",
        )
    verdict = _tcp_probe_from(SEALED_CONTAINER, "1.1.1.1", 443)
    assert verdict != "reached", (
        "the sealed container opened an outbound internet connection while a control from "
        "medos-api shows this host has an uplink. MOS-SEC-029 requires egress deny-all "
        "except the Gateway and the platform callback."
    )
    assert sealed.running
