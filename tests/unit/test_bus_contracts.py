# SPDX-License-Identifier: Apache-2.0
"""The bus contracts that need no database: topics, settings, envelope.

Everything here is a statement chapter 5 makes as a table or a list, checked against the
code that claims to implement it. None of it needs Postgres, so it runs in every pass of
the unit suite rather than only where a stack is up -- which matters because the things
below are the ones that get "tuned" in a values file at three in the morning.

  section 5.8   the closed topic set, and the refusal of modality/nosology topics
  MOS-EXEC-041  the producer settings, clause 3
  MOS-EXEC-042  KRaft only; no ZooKeeper anywhere
  MOS-EXEC-048  the consumer settings table
  MOS-EXEC-075  the event envelope schema

Run:  pytest tests/unit/test_bus_contracts.py -v
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from medos.bus import envelope as env
from medos.bus import topics as T
from medos.bus.kafka import _reject_zookeeper
from medos.bus.port import (
    FORBIDDEN_BROKER_SETTINGS,
    ConsumerConfig,
    ProducerConfig,
)
from medos.db.queue import DISPATCH_SCHEMA_REF, queue_name

REPO = Path(__file__).resolve().parents[2]


# =====================================================================================
# Section 5.8 -- the closed topic set
# =====================================================================================
def test_the_topic_table_is_section_5_8s_table() -> None:
    """Six rows, with the partition counts and retentions chapter 5 assigns them.

    Carried in code so a broker provisioner reads the spec's numbers rather than a Helm
    values file's. The partition count is not cosmetic: it bounds how many consumers of
    one group can make progress at once, and `medicalos.jobs.*` having 12 while
    `medicalos.events.*` has 6 is a deliberate asymmetry between the lifecycle streams and
    the fan-out stream.
    """
    by_name = {t.name: t for t in T.TOPICS}
    assert set(by_name) == {
        "medicalos.jobs.requested", "medicalos.jobs.completed", "medicalos.jobs.failed",
        "medicalos.events.system", "medicalos.events.audit",
        "medicalos.svc.<service_id>.work",
    }
    assert by_name["medicalos.jobs.requested"].partitions == 12
    assert by_name["medicalos.jobs.completed"].partitions == 12
    assert by_name["medicalos.jobs.failed"].partitions == 12
    assert by_name["medicalos.jobs.failed"].retention_days == 30
    assert by_name["medicalos.events.system"].partitions == 6
    assert by_name["medicalos.svc.<service_id>.work"].partitions == 6
    assert by_name["medicalos.svc.<service_id>.work"].retention_days == 1


def test_job_rejected_is_carried_on_completed_and_not_on_failed() -> None:
    """MOS-EXEC-043, which exists because getting it wrong is a clinical error.

    "`job.rejected` is published to `medicalos.jobs.completed`, not to `.failed`, because
    `REJECTED` is a clinical outcome and not an error (MOS-EXEC-014). Consumers MUST switch
    on `envelope.event_type` and MUST NOT infer semantics from the topic name."

    A consumer that filed rejections on the failure topic would report a platform fault
    every time a study had no eligible series.
    """
    assert T.topic_for("job.rejected") == T.JOBS_COMPLETED
    assert T.topic_for("job.completed") == T.JOBS_COMPLETED
    assert T.topic_for("job.failed") == T.JOBS_FAILED
    assert T.topic_for("queue.lease_expired") == T.EVENTS_SYSTEM


def test_the_work_inbox_has_one_name_in_both_drivers() -> None:
    """MOS-EXEC-033: "The spine's per-service work inbox has one name in both drivers."

    Driver 1 spells it as a `job_queue.queue` value and driver 2 as a Kafka topic. One
    name means one definition, which is why `medos.bus.topics` imports driver 1's function
    rather than repeating the f-string. This check exists so that stays true if someone
    "tidies" the import away.
    """
    for service_id in ("medos.slice", "pulmo.effusion", "acme-nodule"):
        assert T.work_topic(service_id) == queue_name(service_id)
        assert T.service_of_work_topic(T.work_topic(service_id)) == service_id


@pytest.mark.parametrize(
    "name",
    [
        "medicalos.ct.requested", "medicalos.mr.requested", "medicalos.xr.completed",
        "medicalos.us.requested", "medicalos.pet.requested",
        "medicalos.chest.work", "medicalos.lung.segmented",
        "medicalos.emphysema.detected", "medicalos.nodule.found",
        "medicalos.effusion.requested", "medicalos.embolism.failed",
    ],
)
def test_modality_and_nosology_topics_are_refused(name: str) -> None:
    """MOS-EXEC-046, with the four reasons chapter 5 calls independently sufficient.

    The one worth repeating here is the second, because it binds this release: spine
    section 14 makes "add lung nodule detection as a second capability with zero core code
    changes" the 0.3.0 gate, and "a nosology topic makes that gate fail by construction: a
    new capability would require a new topic, a new consumer group, new ACLs and a Helm
    change". `zero-core-change` and this refusal are one requirement seen from two ends.
    """
    with pytest.raises(T.ForbiddenTopic):
        T.validate_topic(name)


def test_there_is_no_running_topic_anywhere() -> None:
    """MOS-EXEC-045 and chapter 5's acceptance criterion 21: "`medicalos.jobs.running`
    does not exist and is referenced nowhere in the repository."

    The one permitted occurrence is the refusal itself. Checked over the whole tree
    because the failure this prevents is additive -- someone adds the topic in a values
    file, not in this module.
    """
    with pytest.raises(T.ForbiddenTopic, match="MOS-EXEC-045"):
        T.validate_topic("medicalos.jobs.running")

    hits: list[str] = []
    for path in _repo_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "medicalos.jobs.running" in text:
            hits.append(str(path.relative_to(REPO)))
    allowed = {
        Path("medos/medos/bus/topics.py").as_posix(),
        Path("tests/unit/test_bus_contracts.py").as_posix(),
        Path("medos/medos/db/migrations/0014_outbox.up.sql").as_posix(),
        Path("tests/integration/test_queue_parity.py").as_posix(),
        # The assembled specification, which names the topic in order to forbid it
        # (MOS-EXEC-045). Same exemption `docs/spec/` gets in `_repo_text_files()`; this
        # one lives at the repository root, so the directory filter does not reach it.
        Path("MEDICALOS_SPEC.md").as_posix(),
    }
    unexpected = {h.replace("\\", "/") for h in hits} - allowed
    assert not unexpected, (
        f"MOS-EXEC-045: medicalos.jobs.running is referenced in {sorted(unexpected)}"
    )


def test_a_forbidden_word_is_permitted_only_inside_a_service_id() -> None:
    """Chapter 5's acceptance criterion 20, both halves.

    "No topic name matches `(?i)\\b(ct|mr|...)\\b` except as a substring of a registered
    `service_id` inside `medicalos.svc.<service_id>.work`."

    The exception is real and must work: a vendor service legitimately called
    `pulmo.effusion` would otherwise be unregistrable under driver 2 while registering
    fine under driver 1, which is a parity break that no conformance check at the port
    level would catch.
    """
    assert T.validate_topic("medicalos.svc.pulmo.effusion.work")
    assert T.validate_topic("medicalos.svc.acme.lung-nodule.work")
    assert T.FORBIDDEN_WORD_RE.search("medicalos.svc.pulmo.effusion.work")
    with pytest.raises(T.ForbiddenTopic):
        T.validate_topic("medicalos.effusion.work")


def test_dlq_is_a_sibling_of_a_real_topic_and_never_of_a_dlq() -> None:
    assert T.dlq_of(T.JOBS_FAILED) == "medicalos.jobs.failed.dlq"
    assert T.validate_topic("medicalos.jobs.failed.dlq")
    with pytest.raises(T.ForbiddenTopic):
        T.dlq_of("medicalos.jobs.failed.dlq")


def test_the_partition_key_is_tenant_then_study() -> None:
    """MOS-EXEC-044 and chapter 5's acceptance criterion 22's regex.

    "Every produced record's key matches `^[0-9a-f-]{36}:[0-9.]+$` and equals
    `tenant_id || ':' || study_instance_uid` for job-scoped topics."
    """
    key = T.job_partition_key("00000000-0000-0000-0000-000000000000", "1.2.840.113619.2")
    assert re.match(r"^[0-9a-f-]{36}:[0-9.]+$", key)
    with pytest.raises(ValueError, match="MOS-EXEC-044"):
        T.job_partition_key("not-a-uuid", "1.2.3")
    with pytest.raises(ValueError, match="MOS-EXEC-044"):
        # A study "UID" that is not a UID -- a PatientID pasted into the wrong field is
        # how PHI reaches a partition key, and a partition key reaches every metric label.
        T.job_partition_key("00000000-0000-0000-0000-000000000000", "SMITH^JOHN")


# =====================================================================================
# MOS-EXEC-041 clause 3 and MOS-EXEC-048 -- the settings, as values
# =====================================================================================
def test_the_producer_settings_are_mos_exec_041_clause_3() -> None:
    """"Produce with `enable.idempotence=true`, `acks=all`,
    `max.in.flight.requests.per.connection=5`, `compression.type=zstd`."

    Without idempotence a producer retry after a lost ack writes the record twice, so
    `published_at` would record "the broker acknowledged at least one of my copies"
    instead of "the record is on the log once" -- and clause 4's at-least-once guarantee
    would quietly become unbounded.
    """
    c = ProducerConfig().as_librdkafka(bootstrap_servers="kafka:9092")
    assert c["enable.idempotence"] is True
    assert c["acks"] == "all"
    assert c["max.in.flight.requests.per.connection"] == 5
    assert c["compression.type"] == "zstd"


def test_the_consumer_settings_are_mos_exec_048s_table() -> None:
    c = ConsumerConfig().as_librdkafka(bootstrap_servers="kafka:9092")
    assert c["max.poll.interval.ms"] == 300_000
    assert c["enable.auto.commit"] is False
    assert c["session.timeout.ms"] == 45_000
    assert c["heartbeat.interval.ms"] == 3_000
    assert c["isolation.level"] == "read_committed"
    assert c["auto.offset.reset"] == "earliest"
    assert ConsumerConfig().max_poll_records == 1


def test_raising_max_poll_interval_is_refused() -> None:
    """MOS-EXEC-048's MUST NOT, enforced rather than documented.

    "Implementations MUST NOT respond to this by raising `max.poll.interval.ms`: that only
    lengthens the window during which a genuinely dead consumer holds a partition, and it
    does not help driver 1 at all."

    It is the single most tempting wrong fix on this page -- a chest-CT segmentation takes
    3-20 minutes, the default is 5, and the arithmetic looks conclusive until you notice
    the handler is not supposed to be holding the poll at all (MOS-EXEC-047).
    """
    with pytest.raises(ValueError, match="MOS-EXEC-048"):
        ConsumerConfig(max_poll_interval_ms=1_800_000)
    with pytest.raises(ValueError, match="MOS-EXEC-048"):
        ConsumerConfig(enable_auto_commit=True)
    with pytest.raises(ValueError, match="MOS-EXEC-048"):
        ConsumerConfig(max_poll_records=10)
    with pytest.raises(ValueError, match="MOS-EXEC-048"):
        ConsumerConfig(auto_offset_reset="latest")


def test_zookeeper_is_refused_by_the_client_and_absent_from_every_compose_file() -> None:
    """MOS-EXEC-042: "Kafka MUST be operated in KRaft mode. ZooKeeper mode was removed in
    Kafka 4.0 and MUST NOT appear in any compose file, Helm chart or compatibility
    matrix."
    """
    for setting in FORBIDDEN_BROKER_SETTINGS:
        with pytest.raises(ValueError, match="MOS-EXEC-042"):
            _reject_zookeeper({"bootstrap.servers": "k:9092", setting: "zk:2181"})

    for path in REPO.glob("deploy/**/*.y*ml"):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        assert "zookeeper" not in text, f"{path} mentions ZooKeeper (MOS-EXEC-042)"


# =====================================================================================
# MOS-EXEC-075 -- the envelope
# =====================================================================================
def test_every_schema_keyword_is_one_the_validator_asserts() -> None:
    """The lesson `medos/medos/registry/jsonschema.py` records, applied here.

    A permissive validator silently IGNORES a keyword it does not implement, so a
    constraint can be added to the schema and never checked -- the schema then documents
    a guarantee that does not exist. Rather than a closed keyword set, this module carries
    a narrow validator and this check: every keyword ENVELOPE_SCHEMA uses must be one the
    validator handles.
    """
    used: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                used.add(key)
                if key in {"properties"}:
                    for sub in value.values():
                        walk(sub)
                else:
                    walk(value)

    walk(env.ENVELOPE_SCHEMA)
    # Property NAMES are collected by the walk above too; subtract the ones that are
    # fields rather than keywords.
    used -= set(env.ENVELOPE_SCHEMA["properties"])
    unimplemented = used - env.IMPLEMENTED_KEYWORDS
    assert not unimplemented, (
        f"ENVELOPE_SCHEMA uses {sorted(unimplemented)}, which medos.bus.envelope does "
        "not assert -- the schema would document a constraint nobody checks"
    )


def _valid_envelope() -> dict[str, Any]:
    return {
        "schema_ref": DISPATCH_SCHEMA_REF,
        "event_id": "8b6f0c9e-1d2a-4e3b-9f5c-0a1b2c3d4e5f",
        "event_type": "job.dispatch",
        "event_time": "2026-09-16T10:00:00Z",
        "producer": "medicalos.control-plane@0.3.0",
        "tenant_id": "00000000-0000-0000-0000-000000000000",
        "job_id": "11111111-2222-3333-4444-555555555555",
        "job_seq": 1,
        "partition_key": "00000000-0000-0000-0000-000000000000:1.2.840.113619.2",
        "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        "trace_id": "a" * 32,
        "correlation_id": "job_01H0000000000000000000000",
        "idempotency_key": "ik_" + "a" * 26,
        "attempt": 0,
        "payload": {"service_id": "medos.slice"},
    }


def test_a_conformant_envelope_validates() -> None:
    assert env.envelope_errors(_valid_envelope()) == []


@pytest.mark.parametrize("field", sorted(ENVELOPE_REQUIRED := tuple(
    env.ENVELOPE_SCHEMA["required"]
)))
def test_every_required_field_is_actually_required(field: str) -> None:
    """MOS-TEST-042 (consumer obligation): "`samples/invalid/` MUST contain at least one
    case per required field and per enumerated constraint."

    Generated rather than hand-written, so a field added to `required` cannot arrive
    without its negative case.
    """
    bad = _valid_envelope()
    del bad[field]
    errors = env.envelope_errors(bad)
    assert any(e.startswith(f"{field}: required") for e in errors), errors


def test_the_envelope_is_closed() -> None:
    """`additionalProperties: false`. A consumer that receives an unknown field is a
    consumer reading a message from a producer it does not share a contract with."""
    bad = _valid_envelope() | {"modality": "CT"}
    assert any("not permitted" in e for e in env.envelope_errors(bad))


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_type", "job.running"),                 # not in MOS-EXEC-075's enum
        ("producer", "medos.control-plane"),           # missing the @version
        ("partition_key", "tenant:study"),             # MOS-EXEC-044's shape
        ("trace_id", "ABCD"),                          # 32 lowercase hex
        ("traceparent", "01-" + "a" * 32 + "-" + "b" * 16 + "-01"),  # version must be 00
        ("idempotency_key", "ik_NOTBASE32"),           # ^ik_[a-z2-7]{26}$
        ("job_seq", 0),                                # minimum 1 (MOS-EXEC-078)
        ("attempt", 11),                               # maximum 10
        ("payload", {}),                               # MOS-EXEC-077
        ("event_id", "not-a-uuid"),
        ("event_time", "yesterday"),
    ],
)
def test_each_constraint_is_asserted(field: str, value: Any) -> None:
    bad = _valid_envelope() | {field: value}
    errors = env.envelope_errors(bad)
    assert any(e.startswith(f"{field}: ") for e in errors), (field, errors)


def test_the_bus_envelope_round_trips_to_the_driver_1_shape() -> None:
    """`to_bus_envelope` / `from_bus_envelope` are inverses over the fields driver 1 keeps.

    Driver 2 writes `job_queue.envelope` from a record it consumed, and what it writes
    there must be what driver 1 writes at enqueue, or a runner behaves differently under
    the two drivers while every port-level check still passes.
    """
    dispatch = {
        "schema_ref": DISPATCH_SCHEMA_REF,
        "event_id": "8b6f0c9e-1d2a-4e3b-9f5c-0a1b2c3d4e5f",
        "event_type": "job.dispatch",
        "event_time": "2026-09-16T10:00:00Z",
        "producer": "medos.control-plane",
        "queue": queue_name("medos.slice"),
        "job_id": "job_01H0000000000000000000000",
        "trace_id": "a" * 32,
        "correlation_id": "job_01H0000000000000000000000",
        "payload": {"service_id": "medos.slice", "study_instance_uid": "1.2.3"},
    }
    wire = env.to_bus_envelope(
        dispatch,
        tenant_id="00000000-0000-0000-0000-000000000000",
        job_uuid="11111111-2222-3333-4444-555555555555",
        job_seq=2,
        partition_key="00000000-0000-0000-0000-000000000000:1.2.3",
        attempt=0,
        idempotency_key="ik_" + "a" * 26,
    )
    assert env.envelope_errors(wire) == []
    assert env.from_bus_envelope(wire) == dispatch


def test_the_driver_1_producer_literal_is_the_one_driver_1_writes() -> None:
    """`from_bus_envelope` restores driver 1's `producer` literal, so it must be driver
    1's. Read out of the source rather than restated, because a string constant in two
    files is a string constant that will disagree."""
    source = (REPO / "medos" / "medos" / "db" / "queue.py").read_text(encoding="utf-8")
    assert f'"producer": "{env._DRIVER1_PRODUCER}"' in source


def test_producer_identity_matches_the_required_pattern() -> None:
    """MOS-EXEC-075's `producer` pattern. It is the field that answers "which build
    emitted this", and a value that fails the pattern fails the whole envelope."""
    pattern = env.ENVELOPE_SCHEMA["properties"]["producer"]["pattern"]
    assert re.match(pattern, env.producer_identity())
    assert re.match(pattern, env.producer_identity("outbox-relay"))
    with pytest.raises(ValueError):
        env.producer_identity("Control_Plane")   # [a-z-]+ only


# =====================================================================================
# helpers
# =====================================================================================
def _repo_text_files() -> list[Path]:
    """Every tracked-ish text file, excluding the specification and build detritus.

    `docs/spec/` is excluded because chapter 5 itself names `medicalos.jobs.running` in
    order to forbid it, and a check that failed on the spec would be a check nobody could
    keep green.
    """
    out: list[Path] = []
    skip_dirs = {".git", ".venv", "__pycache__", "node_modules", ".evidence",
                 "docs", ".pytest_cache", ".ruff_cache"}
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix.lower() not in {
            ".py", ".sql", ".yml", ".yaml", ".json", ".md", ".sh", ".toml", ".js", ".ts",
        }:
            continue
        out.append(path)
    return out
