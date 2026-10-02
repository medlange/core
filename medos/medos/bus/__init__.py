# SPDX-License-Identifier: Apache-2.0
"""`medos.bus` -- the event bus `JobQueue` driver 2 rides on.

    port.py       the Producer/Consumer port, and the settings MOS-EXEC-041/048 mandate
    topics.py     section 5.8's CLOSED topic set, and the refusal of modality/nosology ones
    envelope.py   MOS-EXEC-075's envelope schema, its validator, and the outbox adapter
    outbox.py     MOS-EXEC-041's relay: advisory lock, batch, produce, mark, sweep, gauge
    memory.py     an in-process partitioned log with Kafka's delivery semantics
    kafka.py      the `confluent_kafka` binding, imported only when a broker is configured

The `JobQueue` port and both of its drivers live in `medos/medos/db/` -- `queue.py` (driver 1,
Postgres `SKIP LOCKED`) and `queue_kafka.py` (driver 2). That is not a filing accident:
`MOS-EXEC-002` makes Postgres the sole source of truth in BOTH drivers, so driver 2 is a
Postgres driver whose dispatch hop happens to cross a broker, and a driver that lived
away from its port would drift from it.

Nothing here reads the bus to reconstruct state. `MOS-EXEC-002` and `MOS-EXEC-078`:
"Consumers MUST NOT reconstruct job state from the bus at all -- `job_seq` exists so that
a stale event cannot make a DERIVED view flap, not so that the bus can become
authoritative."
"""

from __future__ import annotations

__all__ = ["envelope", "kafka", "memory", "outbox", "port", "topics"]
