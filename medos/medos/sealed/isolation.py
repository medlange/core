# SPDX-License-Identifier: Apache-2.0
"""`Z-SERVICE`'s walls, written as data so that a test can walk into them.

Chapter 8 section 8.1.1 gives the sealed-service zone one row, and the row is short:

    | `Z-SERVICE` | `sealed`-mode service containers | Vendor weights; a Job Token |
    | Ingress from `Z-PLATFORM`; egress to `Z-GATEWAY` and the platform callback only |

and `MOS-SEC-004` says how it is to be held up: "This MUST be enforced by NetworkPolicy
(Kubernetes) or by per-service networks (compose), **not only by configuration of the
client**." Chapter 2's ownership table says the same thing five more times, once per
resource a service must not touch (`MOS-SVC-062` PACS credentials, `MOS-SVC-063` the
database, `MOS-SVC-064` the event bus, `MOS-SVC-065` the object store), and chapter 8's
acceptance check 12 turns it into an executable sentence:

    "From a running `sealed` service container, TCP connections to the PACS port, the
    PostgreSQL port, the Triton port and the object-store port all fail to connect."

WHY THIS IS A MODULE AND NOT A PARAGRAPH IN THE COMPOSE FILE

The deployment already carries one isolation boundary of exactly this shape -- the `pacs`
network of `MOS-DATA-006` -- and the thing that keeps it true is not the comment above it
in `docker-compose.yml`. It is `tests/integration/test_gateway.py`, which opens a TCP
connection from every other container to the PACS's IP address and fails if one succeeds.
This module is the same idea for `Z-SERVICE`, with the destinations and the hardening
rules stated ONCE so that the test, an operator's pre-flight check and any future
Kubernetes NetworkPolicy generator read the same list.

The functions take OBSERVATIONS rather than reaching for docker themselves. That keeps the
module importable with no daemon present, and it keeps the thing being asserted --
"a container whose networks are X, whose user is Y" -- separable from how it was measured.

ONE HONEST GAP, STATED HERE RATHER THAN DISCOVERED LATER. `MOS-SEC-030` requires the
platform to call a sealed service over mTLS with its own workload certificate, and the
compose stack has no mesh and no workload CA. `medos/medos/sealed/invoker.py` refuses a
plaintext base URL unless the deviation is named in the config, which makes the gap a
recorded refusal rather than an unexamined default. Network isolation is not a substitute
for it: isolation stops a third party reaching the service, mTLS stops the service being
driven by something that is not the invoker, and the compose stack relies on the first
alone.

Spec: chapter 8 sections 8.1.1, 8.6 and acceptance check 12; chapter 2 section 2.7;
`MOS-SEC-004`, `MOS-SEC-005`, `MOS-SEC-029`, `MOS-SEC-087`, `MOS-SEC-088`, `MOS-SEC-094`,
`MOS-SEC-096`, `MOS-SEC-119`, `MOS-SVC-025`, `MOS-SVC-050`, `MOS-SVC-062`..`MOS-SVC-065`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "ALLOWED_EGRESS",
    "FORBIDDEN_DESTINATIONS",
    "FORBIDDEN_ENV_SUBSTRINGS",
    "SEALED_NETWORK_SUFFIX",
    "SEALED_SERVICE_CONTAINER",
    "ContainerObservation",
    "Destination",
    "hardening_violations",
    "isolation_violations",
]

#: The compose service, and therefore the container name. One sealed service ships in the
#: reference deployment; a second vendor is a second service on the same network.
SEALED_SERVICE_CONTAINER: Final[str] = "medos-sealed-service"

#: Compose prefixes the project name, so membership is matched on the suffix.
SEALED_NETWORK_SUFFIX: Final[str] = "sealed"

#: The platform-side containers `Z-SERVICE` is allowed to be on a network with, and why.
#: `MOS-SEC-029`: "egress deny-all except the Gateway address and the platform callback
#: address."
ALLOWED_EGRESS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "medos-gateway",
        "Z-GATEWAY",
        "MOS-SVC-025: `network.egress` MUST be a subset of ['dicom-gateway']. It is the "
        "only route by which a sealed service reads a pixel, and it holds the only PACS "
        "credential (MOS-SEC-005).",
    ),
    (
        "medos-worker",
        "Z-PLATFORM",
        "the sealed-service invoker (chapter 2 section 2.6.2 step 2) and the platform "
        "callback address of MOS-SEC-029. Ingress from Z-PLATFORM is the one ingress the "
        "zone table grants; the same membership carries the callback egress.",
    ),
)


@dataclass(frozen=True, slots=True)
class Destination:
    """Something a sealed container must not be able to open a TCP connection to.

    `containers` is a tuple because a destination can be deployed under more than one
    name: the broker is `MOS-REL-023`'s driver 2 and lands in this same release, and a
    checker that only knew one spelling would report "absent" for a Kafka that was there
    under another.
    """

    zone: str
    what: str
    containers: tuple[str, ...]
    ports: tuple[int, ...]
    requirement: str
    why: str


FORBIDDEN_DESTINATIONS: Final[tuple[Destination, ...]] = (
    Destination(
        zone="Z-DATA",
        what="the platform database",
        containers=("medos-postgres",),
        ports=(5432,),
        requirement="MOS-SVC-063, MOS-SEC-004",
        why=(
            "'A service MUST NOT open a connection to PostgreSQL or any platform "
            "datastore.' Every tenancy control in the platform is a row-security policy "
            "on that port; a sealed container that can reach it is one stolen password "
            "away from every tenant, and RLS does not apply to a connection that "
            "authenticates as someone else."
        ),
    ),
    Destination(
        zone="Z-DATA",
        what="the object store",
        containers=("medos-minio",),
        ports=(9000, 9001),
        requirement="MOS-SVC-065, MOS-SEC-087",
        why=(
            "'A service MUST NOT read from or write to the object store. Artifacts move "
            "by platform pull.' MOS-SEC-087 removes the credential; this removes the "
            "route, because a bucket that authorises by network position is reachable by "
            "anything the network lets reach it."
        ),
    ),
    Destination(
        zone="Z-DATA",
        what="the event bus",
        containers=("medos-kafka", "medos-redpanda", "medos-broker", "kafka", "redpanda"),
        ports=(9092, 9093, 29092),
        requirement="MOS-SVC-064, MOS-SEC-096",
        why=(
            "'A service MUST NOT produce to or consume from the event bus.' MOS-SEC-096 "
            "reads the per-service work inbox the same way: it exists so that one "
            "service's backlog cannot block another's, 'not so that vendors get broker "
            "access'. The broker is JobQueue driver 2 and lands in this release; the "
            "route must already be absent when it arrives, not removed afterwards."
        ),
    ),
    Destination(
        zone="Z-INFER",
        what="the shared inference server",
        containers=("medos-triton",),
        ports=(8000, 8001, 8002),
        requirement="MOS-SEC-094",
        why=(
            "'Triton's HTTP and gRPC endpoints MUST be reachable only from Z-PLATFORM. "
            "They MUST NOT be reachable from Z-SERVICE or Z-EDGE. Triton has no tenancy "
            "model and no authentication; its isolation is entirely network and naming.' "
            "It also holds every NATIVE-mode vendor's weights, so a reachable Triton is a "
            "route from one vendor's container to another vendor's model."
        ),
    ),
    Destination(
        zone="Z-PACS",
        what="the PACS",
        containers=("medos-orthanc",),
        ports=(8042, 4242),
        requirement="MOS-SEC-088, MOS-DATA-006",
        why=(
            "'The PACS network address MUST NOT be resolvable or routable from Z-EDGE, "
            "Z-SERVICE or Z-PLATFORM.' The Job Token scopes a service to the dispatched "
            "study (MOS-SEC-028); a direct PACS route is every other study."
        ),
    ),
    Destination(
        zone="Z-PLATFORM",
        what="the residency manager in front of Triton",
        containers=("medos-tritond",),
        ports=(8500,),
        requirement="MOS-SEC-094, MOS-API-001a",
        why=(
            "It serves `/internal/v1/...`, the mesh-internal control surface, which "
            "'authenticates by workload identity or a component-scoped key, never by a "
            "tenant credential' -- and a sealed vendor container holds neither."
        ),
    ),
)

#: Environment-variable name fragments that mean a credential or an address the zone table
#: does not grant. `MOS-SEC-025` is the sharpest of them: the Job Token "MUST NOT be passed
#: as an environment variable or a command-line argument, because both appear in `/proc`,
#: crash dumps and container inspection output" -- which is precisely where this check
#: reads them from.
FORBIDDEN_ENV_SUBSTRINGS: Final[tuple[tuple[str, str], ...]] = (
    ("PASSWORD", "MOS-SEC-005 / MOS-SEC-087: a sealed container holds no credential"),
    ("PASSWD", "MOS-SEC-005"),
    ("SECRET", "MOS-SEC-087"),
    ("DATABASE_URL", "MOS-SVC-063: no route and no DSN to the platform database"),
    ("POSTGRES", "MOS-SVC-063"),
    ("PGHOST", "MOS-SVC-063"),
    ("MINIO", "MOS-SVC-065 / MOS-SEC-087: no object-store credential 'of any kind'"),
    ("S3_", "MOS-SVC-065"),
    ("AWS_", "MOS-SVC-065"),
    ("KAFKA", "MOS-SVC-064: no broker address"),
    ("BOOTSTRAP_SERVERS", "MOS-SVC-064"),
    ("TRITON", "MOS-SEC-094: Triton is not addressable from Z-SERVICE"),
    ("PACS", "MOS-SVC-062 / MOS-SEC-005: the PACS credential reaches one unit"),
    ("ORTHANC", "MOS-SEC-088: the PACS address is not known to Z-SERVICE"),
    ("JOB_TOKEN", "MOS-SEC-025: the Job Token arrives in the request body, never in env"),
    ("ACCESS_TOKEN", "MOS-SEC-025"),
    ("API_KEY", "MOS-SEC-025"),
)


@dataclass(frozen=True, slots=True)
class ContainerObservation:
    """What `docker inspect` says about one container, reduced to what the rules read.

    Constructed by the caller so that this module needs no daemon; the field names match
    the inspect paths they come from, which is how a reader checks the measurement.
    """

    name: str
    networks: Mapping[str, str] = field(default_factory=dict)  # name -> IPAddress
    published_ports: tuple[str, ...] = ()
    user: str = ""
    read_only_root_filesystem: bool = False
    privileged: bool = False
    cap_add: tuple[str, ...] = ()
    cap_drop: tuple[str, ...] = ()
    env_names: tuple[str, ...] = ()
    running: bool = True

    def network_keys(self) -> frozenset[str]:
        """Network names with the compose project prefix stripped to the last segment."""
        return frozenset(name.rsplit("_", 1)[-1] for name in self.networks)


def hardening_violations(sealed: ContainerObservation) -> list[str]:
    """`MOS-SVC-050` and `MOS-SEC-025`, read off the running container.

    "The service container MUST run as a non-root user, with a read-only root filesystem,
    with no `CAP_NET_RAW` or `CAP_SYS_ADMIN`, and with a NetworkPolicy permitting egress
    only to the Gateway and ingress only from the invoker."

    `CAP_NET_RAW` is named in that sentence for a concrete reason and not as boilerplate:
    it is what lets a process craft raw packets, and a container with raw sockets can
    probe and spoof across whatever bridge it is on. Dropping ALL and adding nothing back
    is the only form of this check that does not have to be re-audited when the base image
    changes its default set.
    """
    out: list[str] = []
    if not sealed.running:
        out.append(f"{sealed.name} is not running; nothing below has been established")
        return out

    user = (sealed.user or "").split(":", 1)[0]
    if user in ("", "0", "root"):
        out.append(
            f"{sealed.name} runs as {user or 'the image default (root)'!r}; MOS-SVC-050 "
            "requires a non-root user"
        )
    if not sealed.read_only_root_filesystem:
        out.append(f"{sealed.name} has a writable root filesystem (MOS-SVC-050)")
    if sealed.privileged:
        out.append(f"{sealed.name} is privileged; no capability rule survives that")

    dropped = {c.upper().removeprefix("CAP_") for c in sealed.cap_drop}
    added = {c.upper().removeprefix("CAP_") for c in sealed.cap_add}
    if "ALL" not in dropped:
        out.append(
            f"{sealed.name} does not `cap_drop: [ALL]` (dropped={sorted(dropped)}); "
            "MOS-SVC-050 names NET_RAW and SYS_ADMIN, and an allowlist of two is a "
            "denylist of everything the base image adds next"
        )
    for capability in ("NET_RAW", "SYS_ADMIN"):
        if capability in added:
            out.append(f"{sealed.name} adds CAP_{capability}, which MOS-SVC-050 forbids")

    if sealed.published_ports:
        out.append(
            f"{sealed.name} publishes {sorted(sealed.published_ports)} to the host. The "
            "host is Z-EDGE and the zone table grants Z-SERVICE ingress from Z-PLATFORM "
            "only (MOS-SEC-004); a published port is an ingress from every browser on the "
            "machine."
        )

    for name in sealed.env_names:
        upper = name.upper()
        for fragment, requirement in FORBIDDEN_ENV_SUBSTRINGS:
            if fragment in upper:
                out.append(
                    f"{sealed.name} holds environment variable {name!r} -- {requirement}"
                )
                break
    return out


def isolation_violations(
    sealed: ContainerObservation,
    peers: Mapping[str, ContainerObservation],
) -> list[str]:
    """Network membership against the zone table. The structural half of the proof.

    Structural because it is what makes the LIVE probe's negatives mean something in the
    future as well as today: a TCP probe says "this container cannot reach Postgres now",
    and this says "and nothing that is added to the `medos` network tomorrow will be
    reachable either, because the sealed container is not on it". `MOS-SVC-064`'s broker
    is the live case -- it is not deployed yet, and the route to it must already be absent.
    """
    out: list[str] = []
    if not sealed.running:
        return [f"{sealed.name} is not running; membership cannot be read"]

    keys = sealed.network_keys()
    if keys != {SEALED_NETWORK_SUFFIX}:
        out.append(
            f"{sealed.name} is on networks {sorted(keys)}; Z-SERVICE is on "
            f"{SEALED_NETWORK_SUFFIX!r} and nothing else (MOS-SEC-004)"
        )

    allowed = {name for name, _zone, _why in ALLOWED_EGRESS}
    forbidden_names = {c for dest in FORBIDDEN_DESTINATIONS for c in dest.containers}
    by_container = {dest_container: dest
                    for dest in FORBIDDEN_DESTINATIONS
                    for dest_container in dest.containers}

    for name, peer in peers.items():
        if name == sealed.name:
            continue
        shared = sorted(peer.network_keys() & keys)
        if not shared:
            continue
        if name in forbidden_names:
            dest = by_container[name]
            out.append(
                f"{name} ({dest.zone}, {dest.what}) shares network {shared} with "
                f"{sealed.name}. {dest.requirement}: {dest.why}"
            )
        elif name not in allowed:
            out.append(
                f"{name} shares network {shared} with {sealed.name} and is in neither "
                f"the allowed-egress table nor the forbidden table. Classify it: "
                f"MOS-SEC-004 forbids a zone crossing that is not in the table."
            )

    for name, _zone, why in ALLOWED_EGRESS:
        peer = peers.get(name)
        if peer is None or not peer.running:
            out.append(
                f"{name} is not running, so the sealed container's ONE permitted egress "
                f"cannot be shown to work. {why}"
            )
        elif not (peer.network_keys() & keys):
            out.append(
                f"{name} shares no network with {sealed.name}; the sealed service is "
                f"isolated from the thing it is REQUIRED to reach. {why}"
            )
    return out
