# SPDX-License-Identifier: Apache-2.0
"""The closed topic set, and why there is no modality or nosology topic.

docs/spec/05-execution.md section 5.8. `MOS-EXEC-003`: "The topic set is closed. Adding a
topic is a broker-level operational change and MUST require a spec amendment." This module
is the Python statement of that set; `medos/medos/db/migrations/0014_outbox.up.sql`'s
`job_outbox_topic_ck` is the database statement of it, and
`tests/unit/test_bus_topics.py` compares the two so they cannot drift.

MOS-EXEC-046, THE PART THAT IS EASIEST TO GET WRONG BY BEING HELPFUL
--------------------------------------------------------------------
"Modality topics and nosology topics are forbidden. There MUST NOT be a
`medicalos.ct.requested`, a `medicalos.mr.requested`, a `medicalos.emphysema.*`, or any
topic whose name encodes a modality, body part, technique or clinical finding."

Chapter 5 gives four reasons and calls each independently sufficient. The second is the
one that bears on this release directly: spine section 14 makes "add lung nodule detection
as a second capability with zero core code changes" the 0.3.0 gate, and "a nosology topic
makes that gate fail by construction: a new capability would require a new topic, a new
consumer group, new ACLs and a Helm change." So `zero-core-change` and this module's
refusal are the same requirement seen from two ends.

The first reason is the one that explains the SHAPE of this file: "Routing is data, not
topology." Modality, body part, kernel and slice thickness live in a `ServiceVersion`'s
`SeriesSelector`; nosology lives in a `Capability`. Both are rows. There is therefore
nothing to parameterise a topic name BY, and this module offers no function that would.

The one per-work-unit topic is the per-service inbox, "whose name is the identity of the
party that consumes it -- which is topology, correctly expressed". Its free segment is a
`service_id`, and chapter 5's acceptance criterion 20 explicitly permits a forbidden word
inside one: a service really may be called `pulmo.effusion`. That exception is structural
rather than special-cased, because the service segment is fenced between the literals
`medicalos.svc.` and `.work` and no other member of the closed set has anywhere to put
such a word.

MOS-EXEC-045: there is no `medicalos.jobs.running`. "Intermediate transitions are
observable through `GET /api/v1/jobs/{id}/events`, backed by `job_events` and
`LISTEN`/`NOTIFY` -- never through the bus." It is unrepresentable here, not merely unused.

MOS-EXEC-043: `job.rejected` goes to `medicalos.jobs.completed` and NOT to `.failed`,
because `REJECTED` is a clinical outcome and not an error (`MOS-EXEC-014`). Consumers
"MUST switch on `envelope.event_type` and MUST NOT infer semantics from the topic name",
which is why `topic_for()` is a lookup over event types and never a string built from one.

Spec: MOS-EXEC-003, MOS-EXEC-033, MOS-EXEC-043, MOS-EXEC-044, MOS-EXEC-045, MOS-EXEC-046.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# MOS-EXEC-033: "The spine's per-service work inbox has one name in both drivers,
# `medicalos.svc.<service_id>.work`: in driver 1 it is a value in `job_queue.queue` ... in
# driver 2 it is the Kafka topic of that name." ONE name means one definition, so the
# driver-1 function is imported rather than the f-string repeated. A parity suite whose
# two drivers disagree about the name of the inbox tests nothing.
from medos.db.queue import queue_name as work_topic

__all__ = [
    "TOPICS",
    "Topic",
    "ForbiddenTopic",
    "FORBIDDEN_WORD_RE",
    "JOBS_REQUESTED",
    "JOBS_COMPLETED",
    "JOBS_FAILED",
    "EVENTS_SYSTEM",
    "EVENTS_AUDIT",
    "EVENT_TYPES",
    "dlq_of",
    "is_work_topic",
    "job_partition_key",
    "service_of_work_topic",
    "system_partition_key",
    "topic_for",
    "validate_topic",
    "work_topic",
]


JOBS_REQUESTED: Final = "medicalos.jobs.requested"
JOBS_COMPLETED: Final = "medicalos.jobs.completed"
JOBS_FAILED: Final = "medicalos.jobs.failed"
EVENTS_SYSTEM: Final = "medicalos.events.system"
EVENTS_AUDIT: Final = "medicalos.events.audit"

# The per-service inbox pattern. The service segment is a DNS-ish slug; the fence around
# it is what makes acceptance criterion 20's exception structural (see the module
# docstring).
_WORK_RE: Final = re.compile(
    r"^medicalos\.svc\.(?P<service>[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?)\.work$"
)

# Chapter 5 acceptance criterion 20, verbatim. Applied to a topic name with the
# `service_id` segment removed -- criterion 20 permits a match only there.
FORBIDDEN_WORD_RE: Final = re.compile(
    r"(?i)\b(ct|mr|xr|us|pet|chest|lung|emphysema|nodule|effusion|embolism)\b"
)

_DLQ_SUFFIX: Final = ".dlq"


class ForbiddenTopic(ValueError):
    """A topic outside section 5.8's closed set, or one MOS-EXEC-046 forbids.

    A `ValueError` and not a domain error on purpose: a caller that reaches this has
    constructed a topic name from data, which is the exact move MOS-EXEC-046's first
    reason ("routing is data, not topology") forbids, and it is a programming error rather
    than a runtime condition to be handled.
    """


@dataclass(frozen=True)
class Topic:
    """One row of section 5.8's table.

    `partitions` and `retention_days` are carried so a broker provisioner reads them from
    here rather than from a Helm chart that can disagree with the spec. `event_types` is
    what makes MOS-EXEC-043 checkable: `job.rejected` appears in `medicalos.jobs.completed`
    and in no other row.
    """

    name: str
    event_types: tuple[str, ...]
    partition_key: str  # the shape, for documentation: "<tenant_id>:<study_instance_uid>"
    partitions: int
    retention_days: int


# Section 5.8's table. Six rows; the seventh (`<topic>.dlq`) is derived by `dlq_of()`
# because "same as parent / = parent" is what that row says of its key and partitions.
TOPICS: Final[tuple[Topic, ...]] = (
    Topic(JOBS_REQUESTED, ("job.requested",), "<tenant_id>:<study_instance_uid>", 12, 7),
    # MOS-EXEC-043: job.rejected is HERE, not on .failed.
    Topic(JOBS_COMPLETED, ("job.completed", "job.rejected"),
          "<tenant_id>:<study_instance_uid>", 12, 7),
    Topic(JOBS_FAILED, ("job.failed",), "<tenant_id>:<study_instance_uid>", 12, 30),
    Topic(EVENTS_SYSTEM, ("job.state_changed", "job.step_changed", "queue.lease_expired"),
          "<tenant_id>:<job_id>", 6, 3),
    # Chapter 9 owns the payloads; the retention is "per chapter 9" and is recorded as the
    # audit partition retention this deployment uses rather than invented here.
    Topic(EVENTS_AUDIT, (), "<tenant_id>:<actor_id>", 6, 2555),
    # The per-service inbox. `name` is the template; `work_topic(service_id)` instantiates
    # it. Partitions and retention are section 5.8's for this row.
    Topic("medicalos.svc.<service_id>.work", ("job.dispatch",),
          "<tenant_id>:<study_instance_uid>", 6, 1),
)

# MOS-EXEC-075's `event_type` enum, and MOS-EXEC-077: "adding an event type without a
# payload schema MUST fail CI." Every value here must appear in exactly one TOPICS row
# (audit events excepted -- chapter 9 owns those payloads); a unit test asserts it.
EVENT_TYPES: Final[tuple[str, ...]] = (
    "job.requested", "job.dispatch", "job.state_changed", "job.step_changed",
    "job.completed", "job.rejected", "job.failed", "queue.lease_expired",
)

_EVENT_TO_TOPIC: Final[dict[str, str]] = {
    et: t.name for t in TOPICS for et in t.event_types if not t.name.endswith(".work")
} | {"job.dispatch": "medicalos.svc.<service_id>.work"}


def topic_for(event_type: str, *, service_id: str | None = None) -> str:
    """The topic carrying `event_type`. MOS-EXEC-043.

    A lookup and never a constructed string. `job.rejected` resolves to
    `medicalos.jobs.completed` because "`REJECTED` is a clinical outcome and not an error";
    a consumer that inferred the topic from the name would file every clinical rejection
    as a failure, which is the mistake MOS-EXEC-043 exists to prevent.
    """
    if event_type == "job.dispatch":
        if not service_id:
            raise ForbiddenTopic(
                "job.dispatch is carried on the per-service inbox "
                "`medicalos.svc.<service_id>.work` (MOS-EXEC-033); pass service_id"
            )
        return work_topic(service_id)
    try:
        return _EVENT_TO_TOPIC[event_type]
    except KeyError:
        raise ForbiddenTopic(
            f"{event_type!r} is not one of MOS-EXEC-075's event types {EVENT_TYPES}; "
            "adding one without a payload schema MUST fail CI (MOS-EXEC-077)"
        ) from None


def dlq_of(topic: str) -> str:
    """`<topic>.dlq` -- section 5.8's last row. Same key, same partition count."""
    validate_topic(topic)
    if topic.endswith(_DLQ_SUFFIX):
        raise ForbiddenTopic(f"{topic!r} is already a DLQ; there is no `.dlq.dlq`")
    return topic + _DLQ_SUFFIX


def is_work_topic(topic: str) -> bool:
    base = topic[: -len(_DLQ_SUFFIX)] if topic.endswith(_DLQ_SUFFIX) else topic
    return _WORK_RE.match(base) is not None


def service_of_work_topic(topic: str) -> str:
    """The `service_id` a per-service inbox names. The inverse of `work_topic()`."""
    base = topic[: -len(_DLQ_SUFFIX)] if topic.endswith(_DLQ_SUFFIX) else topic
    m = _WORK_RE.match(base)
    if m is None:
        raise ForbiddenTopic(f"{topic!r} is not a per-service inbox (MOS-EXEC-033)")
    return m.group("service")


def validate_topic(topic: str) -> str:
    """Admit a topic from section 5.8's closed set; refuse everything else.

    MOS-EXEC-003 and MOS-EXEC-046. The refusal is deliberately not a warning and not a
    metric: "Promoting a row to a topic means a registry edit becomes a broker migration",
    and the cost of that is paid once, silently, by whoever comes next.

    The forbidden-word check runs on the name with the `service_id` segment removed,
    because chapter 5's acceptance criterion 20 permits a match there and only there.
    """
    if not isinstance(topic, str) or not topic:
        raise ForbiddenTopic(f"topic must be a non-empty string, got {topic!r}")

    base = topic[: -len(_DLQ_SUFFIX)] if topic.endswith(_DLQ_SUFFIX) else topic

    if base in {JOBS_REQUESTED, JOBS_COMPLETED, JOBS_FAILED, EVENTS_SYSTEM, EVENTS_AUDIT}:
        return topic

    m = _WORK_RE.match(base)
    if m is not None:
        # Criterion 20's exception, and its boundary: the word may be in the service
        # segment. Blank that segment out and the rest must still be clean -- which it is
        # by construction, since the remainder is two literals.
        outside = base.replace(m.group("service"), "", 1)
        if FORBIDDEN_WORD_RE.search(outside):  # pragma: no cover - unreachable literals
            raise ForbiddenTopic(
                f"{topic!r} encodes a modality, body part or finding outside the "
                "service_id segment (MOS-EXEC-046)"
            )
        return topic

    if base == "medicalos.jobs.running":
        raise ForbiddenTopic(
            "MOS-EXEC-045: there is no `medicalos.jobs.running` topic. Intermediate "
            "transitions are read from GET /api/v1/jobs/{id}/events, backed by "
            "job_events -- never from the bus. A .running topic would add a third "
            "ordering-independent stream of a fact Postgres already holds authoritatively."
        )

    if FORBIDDEN_WORD_RE.search(base):
        raise ForbiddenTopic(
            f"MOS-EXEC-046: {topic!r} encodes a modality, body part, technique or "
            "clinical finding. Routing is data, not topology: modality and kernel live "
            "in a ServiceVersion's SeriesSelector and nosology lives in a Capability, "
            "both as rows. A nosology topic also fails the 0.3.0 `zero-core-change` gate "
            "by construction, because a second capability would then need a new topic, a "
            "new consumer group, new ACLs and a Helm change."
        )

    raise ForbiddenTopic(
        f"MOS-EXEC-003: the topic set is closed and {topic!r} is not in it. "
        f"Permitted: {JOBS_REQUESTED}, {JOBS_COMPLETED}, {JOBS_FAILED}, {EVENTS_SYSTEM}, "
        f"{EVENTS_AUDIT}, medicalos.svc.<service_id>.work, and the `.dlq` sibling of any "
        "of those. Adding a topic MUST require a spec amendment."
    )


def job_partition_key(tenant_id: str, study_instance_uid: str) -> str:
    """MOS-EXEC-044: `<tenant_id>:<study_instance_uid>` for every job-scoped topic.

    "This co-locates all work for one study on one partition, which is what makes a 'one
    job per study is already running' check meaningful, and keeps tenants distributed
    across partitions."

    The shape is asserted here and again by `job_outbox.partition_key`'s CHECK, because
    chapter 5's acceptance criterion 22 is an assertion about every PRODUCED record and a
    key that is merely usually right gives the co-location guarantee usually.
    """
    key = f"{str(tenant_id).lower()}:{study_instance_uid}"
    if not re.match(r"^[0-9a-f-]{36}:[0-9.]+$", key):
        raise ValueError(
            f"MOS-EXEC-044: partition key must be <tenant_id>:<study_instance_uid>, "
            f"got {key[:48]!r}"
        )
    return key


def system_partition_key(tenant_id: str, job_id: str) -> str:
    """`<tenant_id>:<job_id>` -- section 5.8's key for `medicalos.events.system`.

    A different shape from `job_partition_key`, and deliberately a different function:
    the system topic orders per JOB, the job-scoped topics order per STUDY, and the two
    give different answers about what may be processed concurrently.
    """
    return f"{str(tenant_id).lower()}:{job_id}"
