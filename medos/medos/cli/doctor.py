# SPDX-License-Identifier: Apache-2.0
"""`medos doctor` -- what this installation can and cannot do right now, and why.

WHY THIS EXISTS
---------------
A developer meeting MedOS for the first time hits a refusal within five minutes. The
console loads and every API call answers `401`. A training submit answers `503
TRAINING_ENVIRONMENT_NOT_RECORDED`. A seal answers `503` naming an environment variable.

Every one of those is correct behaviour, and that is exactly the problem: the platform
answers the same way whether it is REFUSING (it has been asked to make a claim it cannot
back, and saying no is the product working) or merely UNCONFIGURED (nobody has told it
something only a deployment can say). The two are indistinguishable from outside, so the
newcomer concludes the thing is broken and the adopter learns to route around refusals --
which is the one habit this platform must never teach.

This command draws that line. Every row is one of:

  READY         checked, and working.
  UNCONFIGURED  a deployment has not said something only it can say. There is a fix and
                this prints it.
  REFUSING      the platform is declining to make an unbacked claim. There is no fix
                because nothing is broken; the row says what would have to become true.
  UNKNOWN       could not be checked. Reported as unknown and never as ready -- a probe
                that cannot run is not a pass, which is the defect this repository has
                found seven times.

WHAT IT DELIBERATELY DOES NOT DO
---------------------------------
It changes nothing. It sets no variable, writes no row, starts no container. A doctor that
fixed things would be a doctor nobody could run on a production deployment, and this is
most useful exactly there.

It also does not print secrets. A declaration is reported as present or absent and, where
the value is a claim rather than a credential (the de-identification provenance, the
channel map), by its meaning -- never by its bytes.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Status = Literal["READY", "UNCONFIGURED", "REFUSING", "UNKNOWN"]

MARK = {"READY": " ok ", "UNCONFIGURED": " !! ", "REFUSING": " -- ", "UNKNOWN": "  ? "}

#: The service whose environment IS the deployment's environment.
#:
#: WHY THIS IS NOT `os.environ`. The declarations live in docker-compose.yml and are set
#: INSIDE the containers; the shell a developer runs `medos doctor` from has almost none
#: of them. Reading the local environment and reporting the result as the deployment's
#: state is precisely the defect this command exists to expose -- a check describing a
#: state that is not real -- and the first version of this file did exactly that, calling
#: a correctly-declared provenance "REFUSING" because the developer's shell had not heard
#: of it. So the environment is read FROM THE RUNNING SERVICE, and when that cannot be
#: done the source is named in the output rather than silently substituted.
ENVIRONMENT_SERVICE = "medos-api"


@dataclass
class Finding:
    """One thing that is or is not true about this installation."""

    name: str
    status: Status
    detail: str
    #: For UNCONFIGURED: the literal thing to do. For REFUSING: what would have to become
    #: true. Empty for READY.
    remedy: str = ""
    #: What stops working while this is not READY. The reason a developer should care.
    blocks: str = ""


@dataclass
class Section:
    title: str
    findings: list[Finding] = field(default_factory=list)


# --------------------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------------------


def _http(url: str, timeout: float = 4.0) -> tuple[int | None, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return response.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:  # noqa: BLE001 - any failure is "not reachable"
        return None, f"{type(exc).__name__}: {exc}"


def _tcp(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _archive_study_count(env: dict[str, str]) -> tuple[int | None, str]:
    """QIDO the gateway for the study count. `(count, detail)`; count is None on failure.

    NOT the database. `MOS-DATA-006` makes the gateway the only component that may hold a
    PACS credential, so "what is in the archive" is a question the gateway answers and
    nothing else does. Asking Postgres would report what the PLATFORM knows about, which
    is a different and usually smaller set.
    """
    for host, port in (("medos-gateway", 8043), ("127.0.0.1", 8043)):
        tenant = env.get("MEDOS_TENANT_ID") or "00000000-0000-0000-0000-000000000000"
        url = f"http://{host}:{port}/dicomweb/{tenant}/studies"
        token = env.get("MEDOS_DICOMWEB_TOKEN") or env.get("MEDOS_GATEWAY_WORKER_KEY")
        request = urllib.request.Request(url)  # noqa: S310
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=4.0) as response:  # noqa: S310
                if response.status == 204:  # QIDO's "no matches"
                    return 0, url
                return len(json.loads(response.read() or b"[]")), url
        except urllib.error.HTTPError as exc:
            # A 401/403 is a CONFIGURATION answer, not an unreachable gateway, and
            # telling those apart is the whole point of this tool's four states. The
            # caller renders it UNCONFIGURED with the variable to set, never UNKNOWN.
            return None, f"{url} answered {exc.code}"
        except Exception:  # noqa: BLE001 - try the other address
            continue
    return None, "gateway not reachable on medos-gateway:8043 or 127.0.0.1:8043"


#: Each service, at BOTH the address it has on the compose network and the one it has
#: from the host, plus the profile that starts it (empty when it is in the default set).
#:
#: WHY TWO ADDRESSES. This probed `127.0.0.1` at the HOST-PUBLISHED port and nothing else,
#: which is right only when `doctor` runs on the host. Run the way the README suggests --
#: `docker compose exec medos-api python -m medos.cli doctor` -- `127.0.0.1` is the API
#: container's own loopback, so a perfectly healthy viewer answering 200 from the host
#: was reported as "127.0.0.1:3000 refused", with the remedy "docker compose up -d": the
#: command the operator had just run. A checker that reports a working thing as broken
#: and then asks you to repeat yourself is worse than no checker, because the next red
#: row gets ignored too.
#:
#: The ports genuinely differ, which is why one address could not serve: ohif listens on
#: 80 inside and is published on 3000.
#:
#: `orthanc` is deliberately absent. It sits on the `pacs` network with no host port, and
#: `MOS-DATA-006` makes the gateway the only thing that may reach it -- so a probe from
#: here SHOULD fail, and a row that is always red teaches nothing. The gateway's own row
#: covers it: if the gateway is healthy it has reached Orthanc.
SERVICE_PROBES: tuple[tuple[str, tuple[str, int], tuple[str, int], str, str], ...] = (
    ("postgres", ("postgres", 5432), ("127.0.0.1", 5432),
     "every row: cohorts, runs, evidence", ""),
    ("gateway", ("medos-gateway", 8043), ("127.0.0.1", 8043),
     "DICOMweb in and out", ""),
    ("web (viewer origin)", ("web", 80), ("127.0.0.1", 3000),
     "the web surfaces, including the console", ""),
    # Behind `--profile inference` since Triton and MinIO left the default set. Absent is
    # now the NORMAL state, so the remedy names the profile rather than telling an
    # operator to re-run a command that will not start it.
    ("object store", ("minio", 9000), ("127.0.0.1", 9000),
     "sealed manifests and model bundles", "inference"),
)


def check_services(env: dict[str, str]) -> Section:
    section = Section("Services")
    api = env.get("MEDOS_API_BASE", "http://127.0.0.1:8000")
    code, why = _http(f"{api}/readyz")
    if code == 200:
        section.findings.append(Finding("medos-api", "READY", f"{api}/readyz answered 200"))
    elif code is None:
        section.findings.append(Finding(
            "medos-api", "UNCONFIGURED", f"{api} is not reachable ({why})",
            remedy="docker compose -f medos/deploy/compose/docker-compose.yml up -d",
            blocks="everything -- the API is the only way in"))
    else:
        section.findings.append(Finding(
            "medos-api", "UNKNOWN", f"{api}/readyz answered {code}, not 200",
            blocks="readiness is what the suite depends on; a 503 here means a dependency "
                   "of the API is down, not the API itself"))

    for label, inside, outside, blocks, profile in SERVICE_PROBES:
        reached = next((a for a in (inside, outside) if _tcp(*a)), None)
        if reached is not None:
            where = "on the compose network" if reached == inside else "from the host"
            section.findings.append(Finding(
                label, "READY", f"{reached[0]}:{reached[1]} accepts connections ({where})"))
            continue
        tried = " and ".join(f"{h}:{p}" for h, p in (inside, outside))
        section.findings.append(Finding(
            label, "UNCONFIGURED", f"refused on {tried}",
            remedy=(
                f"docker compose -f medos/deploy/compose/docker-compose.yml "
                f"--profile {profile} up -d" if profile else
                "docker compose -f medos/deploy/compose/docker-compose.yml up -d"),
            blocks=blocks))
    return section


#: The declarations a first run meets, in the order it meets them.
#:
#: Not all 56 environment variables the platform reads -- most have sound defaults and a
#: developer never learns their names. These are the ones with no default, or whose
#: absence produces a refusal that reads like a fault.
DECLARATIONS: tuple[tuple[str, str, str, str, bool], ...] = (
    # (variable, what it is, what its absence blocks, remedy, is_a_claim_about_data)
    (
        "MEDOS_DEID_PROVENANCE",
        "what this installation asserts about where its images came from",
        "sealing a cohort: MOS-EVID-021 makes a non-identified DatasetVersion name a "
        "de-identification policy and a UID mapping table, and the seal will not invent "
        "either",
        "declared in medos/deploy/compose/docker-compose.yml with a default for the dev stack. "
        "For real data it must be rewritten for what the archive actually holds.",
        True,
    ),
    (
        "MEDOS_TENANT_SALT",
        "the key patient_key and institution_key are HMAC'd under",
        "pseudonymous keys, and therefore the leakage checks that compare them",
        "declared in medos/deploy/compose/docker-compose.yml with a loud dev "
        "default. A salt in a public repository is not a salt; it is safe only "
        "while the archive holds nothing private.",
        True,
    ),
    (
        "MEDOS_SEAL_STORE",
        "where sealed manifest objects are written",
        "reading a frozen manifest from outside the API process. The only mode available "
        "without it is an in-process memory store that a second process cannot read and "
        "that is lost on restart",
        "set it to an object-store URL. The compose stack runs MinIO on 127.0.0.1:9000 "
        "and nothing points at it.",
        False,
    ),
    (
        "MEDOS_TRAINING_ENVIRONMENT",
        "the nine facts MOS-TRAIN-124 requires recorded about what fitted a model",
        "every training submit: 503 TRAINING_ENVIRONMENT_NOT_RECORDED",
        "two of the nine (code_commit, image_digest) are BUILD facts and must not be "
        "typed into a compose file -- register entry 82 is exactly that mistake. "
        "trainer stamps them into the image at build time.",
        False,
    ),
    (
        "MEDOS_API_VIEWER_AUTHORIZATION",
        "the credential the viewer origin presents to the API",
        "every call the web console makes: 401 AUTHENTICATION_REQUIRED",
        "deliberately has no default. Two honest options: put the page behind the "
        "platform's sign-in, or give the proxy a credential and accept that every act is "
        "then recorded against one shared identity.",
        False,
    ),
    (
        "MEDOS_CHANNEL_MAP_DIR",
        "which raw segment name in which corpus becomes which output channel",
        "building a multi-channel training dataset",
        "point it at a directory of channel tables. "
        "medos/tools/ingest/channel_map_stub.py writes a starting one, with every answer null.",
        True,
    ),
    (
        "MEDOS_PROVENANCE_DIR",
        "per-corpus manifests declaring where ingested studies came from",
        "nothing at runtime, but tests/integration/test_deid_provenance_declaration.py "
        "fails when the archive holds a study no declaration covers",
        "point it at a directory of corpus manifests. "
        "medos/tools/ingest/nrrd_to_dicom.py --manifest writes one.",
        True,
    ),
)


def check_declarations(env: dict[str, str]) -> Section:
    section = Section("Declarations")
    for variable, what, blocks, remedy, is_claim in DECLARATIONS:
        value = (env.get(variable) or "").strip()
        if value:
            detail = what
            if is_claim and variable == "MEDOS_DEID_PROVENANCE":
                try:
                    status = json.loads(value).get("deidentification_status", "?")
                    detail = f"{what} -- declares {status!r}"
                except ValueError:
                    detail = f"{what} -- present but NOT VALID JSON"
                    section.findings.append(Finding(variable, "UNCONFIGURED", detail,
                                                    remedy=remedy, blocks=blocks))
                    continue
            section.findings.append(Finding(variable, "READY", detail))
        else:
            # Absent. Whether that is a refusal or merely unset is the distinction this
            # whole command exists to draw, and it turns on ONE question: would a default
            # be a claim about data that nobody has checked?
            section.findings.append(Finding(
                variable,
                "REFUSING" if is_claim else "UNCONFIGURED",
                what + (
                    "  (no default is possible: any value here is an assertion about "
                    "data, and a wrong one is recorded in an immutable row)" if is_claim else ""
                ),
                remedy=remedy, blocks=blocks))
    return section


def check_data(env: dict[str, str]) -> Section:
    """What the archive holds, and what declares where it came from. Read-only.

    THE FIRST LINE OF THIS DOCSTRING WAS FALSE FOR A LONG TIME. It read "What the archive
    holds. Read-only, and via the API rather than the database" while the function issued
    NO HTTP REQUEST AT ALL -- it globbed two local directories for manifests and channel
    tables and reported on those. So the one command README tells a newcomer to run could
    not tell them their archive was empty, which is the single most likely thing to be
    wrong on a first run, and after `--profile demo` it could not confirm the study had
    landed either.

    The study count now comes from the gateway over QIDO-RS. Not from Postgres:
    `MOS-DATA-006` makes the gateway the only component permitted to hold a PACS
    credential, so it is the only thing that can answer what is actually THERE. The
    database would answer what the platform has been told about.
    """
    section = Section("Data")
    count, where = _archive_study_count(env)
    if count is None and where.endswith(("401", "403")):
        # NOT UNKNOWN. The gateway answered, and what it said is that this process holds
        # no credential for it -- which is a declaration a deployment makes, and which
        # `medos doctor` exists to name. `medos-api` deliberately does not carry
        # MEDOS_DICOMWEB_TOKEN: MOS-DATA-006 keeps PACS credentials to the gateway and
        # its clients, and widening one so a diagnostic reads better is the wrong trade.
        section.findings.append(Finding(
            "archive", "UNCONFIGURED", where,
            remedy="set MEDOS_DICOMWEB_TOKEN to a gateway key with `study.read` -- the "
                   "dev stack's is MEDOS_GATEWAY_WORKER_KEY (default "
                   "`medos-dev-worker-key`), and `medos-worker` already has it",
            blocks="reporting what the archive holds; the archive itself is unaffected"))
    elif count is None:
        section.findings.append(Finding(
            "archive", "UNKNOWN", where,
            blocks="nothing directly, but nothing else here reports whether there are "
                   "images to work on"))
    elif count == 0:
        section.findings.append(Finding(
            "archive", "UNCONFIGURED", f"0 studies ({where})",
            remedy="docker compose -f medos/deploy/compose/docker-compose.yml --profile demo "
                   "up medos-seed-corpus",
            blocks="every job needs a study; an empty archive and a broken stack look "
                   "identical from the viewer"))
    else:
        section.findings.append(Finding(
            "archive", "READY", f"{count} study(ies) ({where})"))
    provenance = Path(env.get("MEDOS_PROVENANCE_DIR") or "medos/deploy/provenance")
    manifests = sorted(provenance.glob("*.json")) if provenance.is_dir() else []
    if manifests:
        covered = 0
        corpora = []
        for path in manifests:
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            covered += len(document.get("study_instance_uids", []))
            corpora.append(document.get("corpus", {}).get("corpus_id", path.stem))
        section.findings.append(Finding(
            "corpus manifests", "READY",
            f"{len(manifests)} manifest(s) covering {covered} study(ies): "
            f"{', '.join(corpora)}"))
    else:
        section.findings.append(Finding(
            "corpus manifests", "UNCONFIGURED",
            f"no *.json under {provenance}",
            remedy="normal for a checkout that has ingested no corpus of its own",
            blocks="a study whose provenance no manifest declares turns the suite red"))

    channel_dir = Path(env.get("MEDOS_CHANNEL_MAP_DIR") or "deploy/channelmap")
    tables = sorted(channel_dir.glob("*.json")) if channel_dir.is_dir() else []
    if tables:
        try:
            # THE ONE PLACE CORE READS THE TRAINING PLANE, and it is deferred, guarded
            # and optional on purpose. A channel map is a training concept; `medos
            # doctor` is a core command that runs on deployments which will never fit a
            # model. Once the training plane is a separate deployable this import fails
            # on a core-only install, and the `ImportError` branch below reports that as
            # a plane that is absent rather than as a broken checkout.
            #
            # It is caught separately from the parse errors underneath it because they
            # mean opposite things: one says "this deployment does not train", the other
            # says "this deployment trains and one of its tables is wrong".
            from medos.training.channelmap import load_channel_map
        except ImportError:
            section.findings.append(Finding(
                "channel declaration", "UNKNOWN",
                f"{len(tables)} table(s) under {channel_dir}, and the training plane is "
                f"not installed to read them",
                blocks="nothing on a core deployment: channel maps are read when a "
                       "dataset is built, which this install cannot do"))
            return section
        try:
            channel_map = load_channel_map({"MEDOS_CHANNEL_MAP_DIR": str(channel_dir)})
            names = channel_map.channels()
            unserveable = [c for c in names if not channel_map.serveable(c)]
            section.findings.append(Finding(
                "channel declaration", "READY",
                f"{len(names)} channel(s) over {len(channel_map.rows)} raw name(s); "
                f"{len(unserveable)} not serveable "
                f"({'none adjudicated' if len(unserveable) == len(names) else 'see status'})"))
        except Exception as exc:  # noqa: BLE001
            section.findings.append(Finding(
                "channel declaration", "UNCONFIGURED", f"{channel_dir}: {exc}",
                remedy="fix or remove the offending table"))
    else:
        section.findings.append(Finding(
            "channel declaration", "UNCONFIGURED", f"no *.json under {channel_dir}",
            remedy="medos/tools/ingest/channel_map_stub.py writes one, with every answer null",
            blocks="building a multi-channel training dataset"))
    return section


def check_trainer(env: dict[str, str]) -> Section:
    """Whether anything on this machine can actually fit a model."""
    section = Section("Training")
    try:
        import subprocess

        out = subprocess.run(
            ["docker", "image", "inspect", env.get("MEDOS_TRAINER_IMAGE",
                                                   "medicalos/trainer:0.3.0.dev0"),
             "--format", "{{.Id}}"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0 and out.stdout.strip():
            section.findings.append(Finding(
                "trainer image", "READY", out.stdout.strip()[:26]))
        else:
            section.findings.append(Finding(
                "trainer image", "UNCONFIGURED", "not built on this machine",
                remedy="trainer/build.sh",
                blocks="fitting a model. The platform carries no torch by design "
                       "(MOS-TRAIN-225); the trainer is a separate image."))
    except Exception as exc:  # noqa: BLE001
        section.findings.append(Finding(
            "trainer image", "UNKNOWN", f"could not ask docker: {type(exc).__name__}"))
    return section


CHECKS: tuple[Callable[[dict[str, str]], Section], ...] = (
    check_services,
    check_declarations,
    check_data,
    check_trainer,
)


def deployment_environment() -> tuple[dict[str, str], str]:
    """The declarations as the RUNNING DEPLOYMENT has them, and where they came from.

    Returns (environment, source). The source is returned rather than hidden because the
    two possible answers mean different things: the container's environment is what the
    platform actually sees, and the local shell's is a developer's guess at it. A report
    that could not tell them apart would be worse than no report.

    The local shell is MERGED OVER the container's, not under it, so that a developer
    exporting a variable to try something sees the effect of what they exported.
    """
    import subprocess

    try:
        out = subprocess.run(
            ["docker", "exec", ENVIRONMENT_SERVICE, "env"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0 and out.stdout:
            container = dict(
                line.split("=", 1) for line in out.stdout.splitlines() if "=" in line
            )
            merged = {
                **container,
                **{k: v for k, v in os.environ.items() if k.startswith("MEDOS_")},
            }
            return merged, f"container {ENVIRONMENT_SERVICE} (+ MEDOS_* from this shell)"
    except Exception:  # noqa: BLE001 - docker absent, container down, permission
        pass
    return dict(os.environ), "THIS SHELL ONLY -- the running deployment was not reachable"


def run(env: dict[str, str] | None = None) -> tuple[list[Section], str]:
    if env is not None:
        return [check(dict(env)) for check in CHECKS], "supplied"
    source, origin = deployment_environment()
    return [check(source) for check in CHECKS], origin


# --------------------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------------------


def render(sections: Sequence[Section], source: str = "") -> str:
    lines: list[str] = ["", "  MedicalOS -- what this installation can do right now"]
    if source:
        lines.append(f"  declarations read from: {source}")
    lines.append("")
    counts: dict[str, int] = {}
    for section in sections:
        lines.append(f"  {section.title}")
        lines.append("  " + "-" * 74)
        for finding in section.findings:
            counts[finding.status] = counts.get(finding.status, 0) + 1
            lines.append(f"   [{MARK[finding.status]}] {finding.name:34s} {finding.detail}")
            if finding.status != "READY":
                if finding.blocks:
                    lines.append(f"         blocks : {finding.blocks}")
                if finding.remedy:
                    label = "would need" if finding.status == "REFUSING" else "to fix"
                    lines.append(f"         {label:7s}: {finding.remedy}")
        lines.append("")

    ready = counts.get("READY", 0)
    unconfigured = counts.get("UNCONFIGURED", 0)
    refusing = counts.get("REFUSING", 0)
    unknown = counts.get("UNKNOWN", 0)
    lines.append("  " + "=" * 74)
    lines.append(f"  {ready} ready   {unconfigured} unconfigured   "
                 f"{refusing} refusing   {unknown} unknown")
    lines.append("")
    if refusing:
        lines.append("  REFUSING is not a fault. Those are declarations only a deployment")
        lines.append("  can make, because any default would be an assertion about data that")
        lines.append("  nobody has checked -- and it would be recorded in a row that is")
        lines.append("  never updated. The platform declining to guess is the product.")
    if unknown:
        lines.append("  UNKNOWN is never counted as ready. A probe that could not run has")
        lines.append("  not passed.")
    lines.append("")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="medos doctor",
        description="Report what this MedicalOS installation can and cannot do, and why.",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(list(argv) if argv is not None else None)

    sections, source = run()
    if args.json:
        print(json.dumps(
            {"environment_source": source,
             "sections": [{"section": s.title,
                           "findings": [vars(f) for f in s.findings]} for s in sections]},
            indent=2,
        ))
    else:
        print(render(sections, source))

    # Exit code says whether anything is UNCONFIGURED -- a state someone can act on.
    # REFUSING does not fail the command: nothing is wrong, and a doctor that exited
    # non-zero on correct behaviour would teach people to ignore its exit code.
    unconfigured = sum(1 for s in sections for f in s.findings if f.status == "UNCONFIGURED")
    return 1 if unconfigured else 0


if __name__ == "__main__":
    raise SystemExit(main())
