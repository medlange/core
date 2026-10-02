# SPDX-License-Identifier: Apache-2.0
"""What `docker compose up -d` starts, asserted against the file rather than the prose.

WHY THIS EXISTS
---------------
Four documents stated the size of the known-defect register and gave three different
answers: README.md said 98 in two places, DEVELOPMENT.md said 99, CONTRIBUTING.md said
98, and the register's highest numbered entry was 100. Nobody was careless; a number
copied into prose is a fact with no owner, and it goes stale the first time the thing it
counts changes.

The service inventory had gone the same way, but worse, because it was not merely stale
-- it named the wrong culprit. DEVELOPMENT.md said "`docker compose up` starts **ten**
services including Triton; there are no profiles yet, so a laptop with no GPU still pulls
the inference plane." Three claims, and each was false:

  * thirteen services are declared, not ten;
  * `profiles: [training]` had already shipped on `medos-trainer`;
  * TRITON HAS NO GPU RESERVATION AT ALL. It carries no `deploy:` block and
    `medos/medos/inference/repository.py` writes `KIND_CPU` instance groups. The nvidia device
    reservation is on `medos-trainer-environment`, which had no profile -- and because
    `medos-api` waited on it and `ohif` waits on `medos-api`, a laptop without an NVIDIA
    runtime got no API and no viewer. The document sent every reader after the wrong
    service, so the real blocker survived for as long as the sentence did.

A wrong diagnosis in the onboarding document is worse than an absent one: it is
load-bearing for somebody else's afternoon.

WHAT IS ASSERTED
----------------
The partition, by parsing `docker-compose.yml` directly -- not by running compose, so
this needs no daemon and runs in the offline suite. Every declared service is in exactly
one bucket, the buckets are named here, and adding a service to the file without deciding
its bucket fails.

And then the documents: each prose count is re-derived and compared. If the register
grows or a service is added, these fail and name the file and the number to change.

Spec: MOS-REL-095 (the documents an adopter looks for), MOS-CONF-109.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
COMPOSE = REPO / "medos" / "deploy" / "compose" / "docker-compose.yml"
REGISTER = REPO / "docs" / "spec" / "99-known-inconsistencies.md"

#: The default set: what `docker compose up -d` starts with no `--profile`. SEVEN, and
#: none of them needs a GPU.
DEFAULT_SERVICES: frozenset[str] = frozenset(
    {
        "postgres",
        "orthanc",
        "medos-gateway",
        "medos-api",
        "medos-worker",
        "web",
        "medos-sealed-service",
    }
)

#: `--profile inference`. A closed subgraph: `minio` <- `medos-model-publish` <-
#: `medos-tritond` -> `triton`, and nothing outside it depends on any of the four.
#: `medos-worker` reaches Triton by environment variable only, with no depends_on edge,
#: and `medos/medos/inference/dispatch.py` returns None from `build_backend()` when
#: MEDOS_TRITON_URL is unset.
INFERENCE_SERVICES: frozenset[str] = frozenset(
    {"triton", "minio", "medos-model-publish", "medos-tritond"}
)

#: `--profile training`. The only two services in the file that need an NVIDIA runtime.
#: `--profile training`. Two of these need an NVIDIA runtime; `medos-train-api` does
#: not -- it is the MODEL-PREPARATION SERVICE, the second of the two products, and it
#: runs `medos.api.training_plane:create_training_app` rather than
#: `medos.api.app:create_app`. It sits behind this profile because a deployment that
#: only serves models should not be able to prepare one, not because it needs a GPU.
TRAINING_SERVICES: frozenset[str] = frozenset(
    {"medos-trainer-environment", "medos-trainer", "medos-train-api"}
)

#: `--profile demo`. One one-shot that fabricates a synthetic CT study and exits.
#:
#: Behind a profile because seeding is a CHOICE. The profile is not the real guard,
#: though -- `medos/tools/demo/seed_corpus.py` refuses unless `MEDOS_ENV` is exactly `dev`, and
#: that refusal is what survives somebody copying this compose file and editing the
#: database URL. `test_the_seeder_refuses_outside_a_dev_deployment` is where that is
#: asserted; a profile is convenience and a refusal is a control.
DEMO_SERVICES: frozenset[str] = frozenset({"medos-seed-corpus"})


def _compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _service_blocks() -> dict[str, list[str]]:
    """Service name -> its block's lines. A two-space key under `services:`.

    Parsed rather than loaded with a YAML library on purpose: the file uses merge keys and
    anchors heavily, and a loader resolves them, which would make `<<: *anchor` invisible
    to a reader of this test. Indentation is what the file means.
    """
    lines = _compose_text().split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "services:")
    except StopIteration:  # pragma: no cover - the file would be unrecognisable
        pytest.fail("docker-compose.yml has no top-level `services:` key")

    blocks: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines[start + 1 :]:
        if line and not line.startswith(" ") and line.rstrip().endswith(":"):
            break  # a new top-level key: volumes, networks, ...
        match = re.match(r"^  ([a-z0-9][a-z0-9_-]*):\s*$", line)
        if match:
            current = match.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return blocks


def _profiles_of(block: list[str]) -> set[str]:
    for line in block:
        match = re.match(r"^    profiles:\s*\[([^\]]*)\]\s*$", line)
        if match:
            return {p.strip() for p in match.group(1).split(",") if p.strip()}
    return set()


# --------------------------------------------------------------------------------------
# the partition
# --------------------------------------------------------------------------------------


def test_every_declared_service_is_in_exactly_one_named_bucket() -> None:
    """A new service with no decision about its profile fails here rather than silently
    joining the default set, which is the direction that costs a stranger a download."""
    declared = set(_service_blocks())
    named = DEFAULT_SERVICES | INFERENCE_SERVICES | TRAINING_SERVICES | DEMO_SERVICES
    assert declared == named, (
        f"declared but not bucketed: {sorted(declared - named)}\n"
        f"bucketed but not declared: {sorted(named - declared)}\n"
        "Every service belongs to the default set or to a profile, and which one is a "
        "decision, not a default."
    )
    buckets = {
        "default": DEFAULT_SERVICES,
        "inference": INFERENCE_SERVICES,
        "training": TRAINING_SERVICES,
        "demo": DEMO_SERVICES,
    }
    overlap: set[str] = set()
    for first in buckets:
        for second in buckets:
            if first < second:
                overlap |= buckets[first] & buckets[second]
    assert not overlap, f"a service is in two buckets: {sorted(overlap)}"


def test_the_default_set_declares_no_profile_and_the_others_do() -> None:
    blocks = _service_blocks()
    for name in sorted(DEFAULT_SERVICES):
        got = _profiles_of(blocks[name])
        assert not got, f"{name} is in the default set but declares profiles {sorted(got)}"
    for name in sorted(INFERENCE_SERVICES):
        assert _profiles_of(blocks[name]) == {"inference"}, name
    for name in sorted(TRAINING_SERVICES):
        assert _profiles_of(blocks[name]) == {"training"}, name
    for name in sorted(DEMO_SERVICES):
        assert _profiles_of(blocks[name]) == {"demo"}, name


def test_no_default_service_reserves_a_gpu() -> None:
    """THE ONE THAT WOULD HAVE CAUGHT IT. A device reservation fails at container-create,
    before the container's environment is read -- which is why the documented
    `MEDOS_TRAINER_ALLOW_CPU=1` escape hatch could never be reached on a machine with no
    NVIDIA runtime. A GPU anywhere in the default set takes out `docker compose up` for
    everyone who does not have one."""
    blocks = _service_blocks()
    for name in sorted(DEFAULT_SERVICES):
        # COMMENTS STRIPPED FIRST. The first version of this check read the raw block and
        # failed on the paragraph above `medos-api`'s depends_on, which explains why the
        # `&trainer-gpu` edge was removed. A check that fires on a comment ABOUT the thing
        # it forbids teaches people to stop writing the comment.
        body = "\n".join(
            line for line in blocks[name] if not line.lstrip().startswith("#")
        )
        for marker in ("driver: nvidia", "runtime: nvidia", "gpus:", "trainer-gpu"):
            assert marker not in body, (
                f"{name} is started by a bare `docker compose up -d` and references "
                f"{marker!r}. On a host with no NVIDIA container runtime this service "
                f"cannot be created, and anything that depends on it never starts."
            )


def test_nothing_in_the_default_set_depends_on_a_profiled_service() -> None:
    """Compose rejects the WHOLE PROJECT for this, not just the one service: `service "x"
    depends on undefined service "y": invalid compose project`. So this is not a style
    rule -- an unprofiled service depending on a profiled one breaks `up` for everybody,
    including the people it works for today."""
    blocks = _service_blocks()
    profiled = INFERENCE_SERVICES | TRAINING_SERVICES | DEMO_SERVICES
    for name in sorted(DEFAULT_SERVICES):
        inside = False
        for line in blocks[name]:
            if re.match(r"^    depends_on:", line):
                inside = True
                continue
            if inside:
                if re.match(r"^    \S", line):
                    inside = False
                    continue
                dep = re.match(r"^      ([a-z0-9][a-z0-9_-]*):\s*$", line)
                if dep and dep.group(1) in profiled:
                    pytest.fail(
                        f"{name} is in the default set and depends on {dep.group(1)}, "
                        f"which is behind a profile. Compose rejects the entire project "
                        f"file for this, so `docker compose up -d` stops working."
                    )


def test_the_triton_url_default_is_empty() -> None:
    """Pointing a worker at `http://triton:8000` when Triton was not started does not give
    a worker without inference; it gives one that builds a TritonBackend and finds out per
    job. `build_backend()` returning None is the honest state."""
    text = _compose_text()
    assert 'MEDOS_TRITON_URL: "${MEDOS_TRITON_URL-}"' in text, (
        "medos-worker's MEDOS_TRITON_URL should default to empty now that Triton is "
        "behind `--profile inference`."
    )


# --------------------------------------------------------------------------------------
# the prose
# --------------------------------------------------------------------------------------


def _register_entry_numbers() -> list[int]:
    r"""Every numbered entry in the register, in file order.

    `\S`, not `[A-Z]`. The pattern required a capital letter immediately after the number
    and THIRTY-ONE entries do not have one -- they open with a code span: ``2.``, ``10.``
    through ``13.``, ``59.``-``62.``, ``110.``-``112.`` and more. The register's own
    convention permits it; the check did not, and so it had been reading 83 of 116 entries
    and calling the largest of those the size.
    """
    return [
        int(m.group(1))
        for m in re.finditer(r"^(\d+)\. \S", REGISTER.read_text(encoding="utf-8"), re.M)
    ]


def _register_entry_count() -> int:
    """The COUNT, not the highest number. The two differ, and that is a defect of its own.

    This returned `max(numbers)`. With no duplicates and no gaps the two agree, which is
    why nobody noticed -- and the register HAS duplicates: entries 89 and 90 are each used
    by two unrelated entries. `test_register_entry_numbers_are_unique` below is the
    witness. The documents claim a number of "recorded places", so the count is what they
    are claiming.
    """
    numbers = _register_entry_numbers()
    assert numbers, "no numbered entries found in the register"
    return len(numbers)


#: The phrasings a document uses to state the register's size. NOT paired with a filename
#: any more, and that change is the point.
#:
#: THIS GATE WAS THE DEFECT IT WAS WRITTEN TO FIX. It took a hand-written list of four
#: (document, pattern) pairs, so it checked exactly the four sites I knew about when I wrote
#: it -- and measured on 2026-09-26, TWO MORE existed and were stale: `medos/README.md` said
#: 113 and `SECURITY.md` said 98, against a register holding 130. A gate given a list of
#: sites cannot see a site that is not on the list, which is the same shape as the claim it
#: guards. It now ENUMERATES: every tracked markdown file outside the specification is
#: searched for every phrasing, so a new document stating the count is covered the day it
#: is written, and a new PHRASING is the only thing that can still escape.
REGISTER_COUNT_PHRASINGS: tuple[str, ...] = (
    r"(\d+) recorded places",
    r"records (\d+) places",
    r"would resolve it\. (\d+) entries",
    r"It has (\d+) entries",
    r"records (\d+)\b(?![\d.])",
)

#: Documents whose register-size claim is HISTORY and must not be updated: a release record
#: states what was true at its own tag. `docs/README.md` says so in terms.
REGISTER_COUNT_EXEMPT: tuple[str, ...] = ("docs/releases/",)


def _documents_stating_the_register_size() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    # UNTRACKED FILES COUNT: a new document stating a wrong register size should
    # fail while its author is still writing it, not on the commit that adds it.
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.split()
    for rel in tracked:
        if rel.startswith("docs/spec/") or any(
            rel.startswith(x) for x in REGISTER_COUNT_EXEMPT
        ):
            continue
        text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        if "99-known-inconsistencies" not in text and "register" not in text.lower():
            continue
        found: list[str] = []
        for pattern in REGISTER_COUNT_PHRASINGS:
            for m in re.finditer(pattern, text):
                # only a sentence that is about the register
                window = text[max(0, m.start() - 260):m.end() + 60]
                if "99-known-inconsistencies" in window or "register" in window.lower():
                    found.append(m.group(1))
        if found:
            out[rel] = found
    return out


def test_the_register_size_is_stated_somewhere_to_check() -> None:
    """A green run must not be reachable by finding no claims at all."""
    sites = _documents_stating_the_register_size()
    assert len(sites) >= 4, (
        f"only {len(sites)} document(s) found stating the register size: {sorted(sites)}. "
        "Before this gate enumerated, it checked a hand-written list of four and missed two."
    )


def test_every_document_states_the_true_register_size() -> None:
    actual = _register_entry_count()
    wrong = {
        rel: [c for c in claims if int(c) != actual]
        for rel, claims in _documents_stating_the_register_size().items()
    }
    wrong = {k: v for k, v in wrong.items() if v}
    assert not wrong, (
        f"{len(wrong)} document(s) state a register size that is not {actual}: {wrong}. "
        f"This is the COUNT, not the highest number -- they differ by the duplicated "
        f"entries 89 and 90."
    )


def test_the_readme_states_the_true_default_service_count() -> None:
    text = (REPO / "README.md").read_text(encoding="utf-8")
    match = re.search(r"brings up \*\*(\w+)\*\* services", text)
    assert match, "README.md no longer states how many services `up -d` brings up"
    words = {"seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
    claimed = words.get(match.group(1).lower())
    assert claimed == len(DEFAULT_SERVICES), (
        f"README.md says `up -d` brings up {match.group(1)} services; the default set "
        f"holds {len(DEFAULT_SERVICES)}."
    )


def test_every_service_that_trains_asks_for_the_shared_memory_it_needs() -> None:
    """A GPU reservation without `shm_size` is a feeder starved for the accelerator it feeds.

    nnU-Net runs its data augmentation in worker processes that hand batches back through
    /dev/shm. Docker gives a container 64 MiB of it by default, which is smaller than one
    96x160x160 patch batch of Dataset512_MedOS10Big — so the workers die and the trainer
    reports only:

        RuntimeError: One or more background workers are no longer alive. Exiting.
        Please check the print statements above for the actual error message

    That names the symptom, not the cause, and there is nothing above it to check. It also
    arrives AFTER the run has been configured and started, which is the expensive moment to
    discover a container setting.

    Observed on this stack: a 1-epoch smoke run died this way in under twenty seconds, and
    the identical command with `--shm-size=8g` reached Epoch 0. `medos-trainer` reserved a
    GPU and never asked for the memory that GPU's feeder needs.

    The rule is the narrow one the evidence supports: a service that reserves an NVIDIA
    device AND runs a training command must declare `shm_size`. Services that reserve a
    device only to inspect it — `medos-trainer-environment` writes one JSON file and exits —
    load no data and need none.
    """
    blocks = _service_blocks()
    offenders = []
    for name, lines in blocks.items():
        body = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
        reserves_gpu = "driver: nvidia" in body or "*trainer-gpu" in body
        # The distinguishing mark of a service that LOADS data, as against one that only
        # asks the driver what it can see.
        trains = "execute" in body and "medicalos/trainer" in body
        if reserves_gpu and trains and "shm_size" not in body:
            offenders.append(name)

    assert not offenders, (
        f"these reserve a GPU and run a training command without declaring shm_size, so "
        f"their augmentation workers die on Docker's 64 MiB default: {offenders}"
    )


def test_a_requirement_the_adr_calls_still_binding_has_a_register_entry() -> None:
    """A document that names the entry another document should carry must be checked.

    `docs/adr/BUILD_VS_ADOPT.md` reached this conclusion in its own words — "`MOS-UI-009`
    ... still forbids the clinician surface to implement windowing, stack scrolling, MPR or
    any viewport rendering of pixel data. It is the one requirement that actually binds, it
    has not been withdrawn" — and then said, explicitly, that the honest register entry for
    every viewer feature is "chosen, and forbidden by `MOS-UI-009` until that is withdrawn".

    The register had no such entry through twelve viewer commits. Nobody was careless: the
    sentence naming the obligation and the file carrying it were never compared, which is
    the same shape as the four documents that gave three different register sizes and that
    §99 exists because of.

    The rule is narrow and mechanical: where the ADR says a requirement STILL FORBIDS
    something or HAS NOT BEEN WITHDRAWN, the register must name that requirement.

    WHAT CHANGED UNDER THIS GATE, AND WHY IT STILL STANDS. `MOS-UI-009` was WITHDRAWN at
    specification 0.3.0 and replaced by `MOS-UI-009a`; `MOS-UI-205` and `MOS-UI-373` went
    at 0.4.0. The ADR sentences this scans are therefore HISTORICAL -- they record what the
    decision record believed when it was written, and they are kept struck-through-in-place
    rather than deleted for the same reason the specification keeps withdrawn requirements
    visible. The gate keeps passing because the register does name them: entry 103 for
    `MOS-UI-009`, entry 106 for the three that survived it.

    So this gate now guards a different thing than it was written for, and that is the
    point of leaving it: if somebody later cleans those sentences out of the ADR, the first
    assertion below fires with a message telling them to re-read rather than relax. Re-point
    it at whatever the ADR then claims is binding. Do not delete it -- the failure it exists
    to catch is a document naming an obligation that no file carries, and that failure does
    not go away because this instance of it was closed.
    """
    adr = (REPO / "docs" / "adr" / "BUILD_VS_ADOPT.md").read_text(encoding="utf-8")
    register = REGISTER.read_text(encoding="utf-8")

    binding: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+", adr):
        if not re.search(
            r"still forbid|has not been withdrawn|until that is withdrawn", sentence, re.I
        ):
            continue
        binding.update(re.findall(r"MOS-[A-Z]+-\d+", sentence))

    assert binding, (
        "no requirement in the ADR is described as still binding, which would mean the "
        "standing note about MOS-UI-009 has been reworded — re-read it before relaxing "
        "this gate"
    )

    missing = sorted(r for r in binding if r not in register)
    assert not missing, (
        f"the ADR says these still bind and have not been withdrawn, and the register does "
        f"not name them — so the repository's own statement of where it stands omits the "
        f"requirement its architecture document calls the one that actually binds: {missing}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="register entry 115: entries 89 and 90 are each used by two unrelated entries. "
           "Fixing this turns THIS TEST RED -- close register entry 115 and delete the "
           "xfail marker.",
)
def test_register_entry_numbers_are_unique() -> None:
    r"""A number that names two entries is a citation that names neither.

    `medos/medos/api/routes_curation.py:1220` ends a paragraph with "Register entry 90.",
    and there are two of those. The reader has to read both to find out which.

    This went unseen because the check beside it matched `^(\d+)\. [A-Z]` and both
    duplicates open with a code span. A pattern that reads 83 of 116 entries cannot report
    a collision between two of the 33 it skips.
    """
    numbers = _register_entry_numbers()
    seen: dict[int, int] = {}
    for n in numbers:
        seen[n] = seen.get(n, 0) + 1
    duplicated = sorted(n for n, k in seen.items() if k > 1)
    assert not duplicated, (
        f"these register numbers name more than one entry: {duplicated}. A citation of "
        "the form 'register entry N' then names neither"
    )


# =====================================================================================
# The Postgres healthcheck names a migration, and a named version goes stale
# =====================================================================================
MIGRATIONS = REPO / "medos" / "medos" / "db" / "migrations"


def _highest_migration() -> str:
    versions = sorted(p.name[: -len(".up.sql")] for p in MIGRATIONS.glob("*.up.sql"))
    assert versions, f"no migrations under {MIGRATIONS}; this check reads nothing"
    return versions[-1]


def test_the_postgres_healthcheck_probes_the_LAST_migration() -> None:
    """A readiness probe that names a version is only true until the next version.

    The probe used to be `pg_isready`, which answers "the server accepts connections"
    and nothing else -- measured exit 0 for a role that does not exist AND a database
    that does not exist, while the comment beside it claimed it meant "schema.sql has
    been applied". During initdb the temporary server listens on the very socket
    pg_isready defaults to, so with `start_period: 5s` against a 56-second migration run
    every service waiting on `service_healthy` was released about fifty seconds early.
    Register entry 119.

    The replacement asks whether the LAST migration is recorded, which is a real
    question -- and one that quietly stops being the last question the moment somebody
    adds `0016_*.up.sql`. That is the same defect in a new place: a check that passes
    while the schema is still being built. So the version is asserted HERE, against the
    directory, rather than trusted to be remembered.
    """
    latest = _highest_migration()
    text = _compose_text()

    probe = [
        line for line in text.splitlines()
        if "schema_migrations" in line and "CMD-SHELL" in line
    ]
    assert len(probe) == 1, (
        f"expected exactly one healthcheck probing `schema_migrations`, found "
        f"{len(probe)}. If the Postgres healthcheck went back to `pg_isready`, read "
        "register entry 119 before deciding that is fine."
    )

    assert f"'{latest}'" in probe[0], (
        f"the Postgres healthcheck probes for a migration that is no longer the last "
        f"one. The newest on disk is {latest!r}:\n    {probe[0].strip()}\n"
        "  Until this names it, the stack reports healthy as soon as the SECOND-newest "
        "migration lands, and every service that waits on `service_healthy` starts "
        "against a schema that is still being built -- which is exactly what entry 119 "
        "records happening with `pg_isready`."
    )


# ======================================================================================
# CONTRACT.md section 1, which calls itself authoritative about the repository layout
# ======================================================================================
#: The `### <word> that ...` headings in CONTRACT.md §1, each followed by a fenced block
#: whose entries are top-level paths. The heading COUNTS the entries, and it stopped
#: matching: "Three that belong to no product" listed two, because `spikes/week0/` was
#: deleted and the entry went with it while the count did not. A section that calls itself
#: authoritative and counts a directory that is not there is the first place somebody looks.
NUMBER_WORDS: dict[str, int] = {
    "One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5, "Six": 6, "Seven": 7,
}


def _contract_layout_sections() -> dict[str, tuple[int, list[str]]]:
    """heading -> (the count its first word states, the paths its fenced block lists)."""
    text = (REPO / "CONTRACT.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    out: dict[str, tuple[int, list[str]]] = {}
    # `### Four products` AND `### Two that belong to no product`. The first draft required
    # the word "that" and so covered only the second of §1's two blocks -- it read half its
    # own subject, which is the defect this whole round is about one size smaller.
    for m in re.finditer(r"^### (\w+) (products|that [^\n]+)$", text, re.M):
        word = m.group(1)
        if word not in NUMBER_WORDS:
            continue
        block = re.search(r"\n```\n(.*?)\n```", text[m.end():], re.S)
        if not block:
            continue
        entries = [
            line.split()[0] for line in block.group(1).split("\n")
            if line and not line.startswith(" ") and "/" in line.split()[0]
        ]
        out[m.group(0)] = (NUMBER_WORDS[word], entries)
    return out


def test_the_authoritative_layout_counts_what_it_lists() -> None:
    sections = _contract_layout_sections()
    assert sections, "CONTRACT.md §1's layout headings no longer parse"
    wrong = {
        heading: (stated, entries)
        for heading, (stated, entries) in sections.items()
        if stated != len(entries)
    }
    assert not wrong, (
        "CONTRACT.md §1 headings whose number does not match the block beneath them: "
        + "; ".join(
            f"{h!r} says {s} and lists {len(e)} ({e})" for h, (s, e) in wrong.items()
        )
    )


def test_every_path_the_authoritative_layout_lists_exists() -> None:
    absent: list[str] = []
    for heading, (_, entries) in _contract_layout_sections().items():
        for entry in entries:
            if not (REPO / entry.rstrip("/")).exists():
                absent.append(f"{entry} (under {heading!r})")
    assert not absent, (
        "CONTRACT.md §1 calls itself authoritative and lists paths that do not exist: "
        f"{absent}"
    )


# ======================================================================================
# Every document that counts the compose services, not just README.md
# ======================================================================================
#: MEASURED: `test_the_readme_states_the_true_default_service_count` above reads README.md
#: and nothing else, and `DEVELOPMENT.md` carried "Thirteen services are declared; **seven**
#: start by default. The other six sit behind two profiles" against a file declaring FIFTEEN,
#: seven of them default and EIGHT profiled. Two of its three numbers were wrong.
#:
#: WHAT MADE IT INVISIBLE, and it is the reason this check exists rather than a corrected
#: number: 13 = 7 + 6, so the sentence's own arithmetic was consistent. Two services were
#: added afterwards and the sentence stayed internally coherent while drifting from the tree.
#: Nothing reads wrong in a self-consistent sentence -- which is exactly why a count in prose
#: needs a check and not a proofread. Entry 129's lesson supplies the other half: this
#: enumerates the documents rather than naming README.md.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
}

#: phrasing -> which population it counts
SERVICE_COUNT_CLAIMS: tuple[tuple[str, str], ...] = (
    (r"(\w+) services are declared", "declared"),
    (r"brings up \*\*(\w+)\*\* services", "default"),
    (r"The other (\w+) sit behind", "profiled"),
)


def _documents_counting_services() -> dict[str, list[tuple[str, str, int]]]:
    out: dict[str, list[tuple[str, str, int]]] = {}
    tracked = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "*.md"],
        cwd=REPO, capture_output=True, text=True,
    ).stdout.split()
    for rel in tracked:
        if rel.startswith(("docs/spec/", "docs/releases/")):
            continue
        text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        hits: list[tuple[str, str, int]] = []
        for pattern, population in SERVICE_COUNT_CLAIMS:
            for m in re.finditer(pattern, text):
                token = m.group(1).lower()
                value = _NUMBER_WORDS.get(token)
                if value is None and token.isdigit():
                    value = int(token)
                if value is None:
                    continue
                hits.append((m.group(0), population, value))
        if hits:
            out[rel] = hits
    return out


def test_some_document_counts_the_services() -> None:
    """A green run must not be reachable by finding no claims at all."""
    sites = _documents_counting_services()
    assert sites, (
        "no document was found counting the compose services; the phrasings in "
        "SERVICE_COUNT_CLAIMS no longer match anything, which is how a check stops checking"
    )


def test_every_document_counts_the_services_correctly() -> None:
    # From the four sets this module already declares and already asserts the partition of,
    # rather than a second parse of the compose file. One source of truth: if a service is
    # added and its bucket is not declared, the partition test above fails first and names
    # it, which is a better message than a count that disagrees by one.
    profiled = INFERENCE_SERVICES | TRAINING_SERVICES | DEMO_SERVICES
    truth = {
        "declared": len(DEFAULT_SERVICES | profiled),
        "default": len(DEFAULT_SERVICES),
        "profiled": len(profiled),
    }
    wrong: dict[str, list[str]] = {}
    for rel, hits in _documents_counting_services().items():
        bad = [
            f"{phrase!r} claims {value} {population}, measured {truth[population]}"
            for phrase, population, value in hits
            if value != truth[population]
        ]
        if bad:
            wrong[rel] = bad
    assert not wrong, (
        f"{sum(len(v) for v in wrong.values())} service count(s) disagree with "
        f"docker-compose.yml ({truth}):\n  "
        + "\n  ".join(f"{rel}: {bad}" for rel, bad in sorted(wrong.items()))
    )
