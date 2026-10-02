# SPDX-License-Identifier: Apache-2.0
"""`MOS-REG-059`'s canary bucket: CRC-32/Castagnoli over `tenant_id || 0x00 || study_id`.

WHY CRC-32C AND NOT `zlib.crc32`
--------------------------------
`MOS-REG-059` names CRC-32/Castagnoli. `zlib.crc32` is CRC-32/ISO-HDLC (the "IEEE"
polynomial 0xEDB88320) and produces a different value for the same bytes, so a platform
that used it would bucket studies differently from every other implementation of this
spec and from its own recorded `canary_bucket` values after a language change. The
Castagnoli polynomial (reversed 0x82F63B78) is implemented here, table-driven, in ~15
lines of stdlib -- rather than adding a dependency to `requirements-dev.txt` mid-block,
which breaks every agent who has not reinstalled.

Verified against the standard vectors in `tests/integration/test_resolution.py`:
`crc32c(b"") == 0`, `crc32c(b"123456789") == 0xE3069283`.

WHY THE STUDY ID AND NOT THE JOB ID
------------------------------------
`MOS-REG-059`, verbatim: "Using the study id rather than the job id guarantees that every
attempt of every job for one study lands in the same bucket, and therefore that a retry
cannot cross the canary boundary." `MOS-REG-079` is the same fact stated as a property:
canary traffic is study-sticky.

`study_id` is "the platform's internal study identifier, never the `StudyInstanceUID`, so
no PHI enters the hash".

Pure: stdlib only, no clock, no randomness.
"""

from __future__ import annotations

from typing import Final

__all__ = ["BUCKETS", "canary_bucket", "crc32c", "deployment_rank"]

BUCKETS: Final[int] = 1000

_POLY: Final[int] = 0x82F63B78  # CRC-32C, reversed representation


def _make_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ (_POLY if crc & 1 else 0)
        table.append(crc)
    return tuple(table)


_CRC_TABLE: Final[tuple[int, ...]] = _make_table()


def crc32c(data: bytes) -> int:
    """CRC-32/Castagnoli, the value `MOS-REG-059` hashes the bucket from."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


def canary_bucket(tenant_id: str, study_id: str) -> int:
    """`crc32c(tenant_id || 0x00 || study_id) mod 1000`, verbatim from `MOS-REG-059`."""
    material = tenant_id.encode("utf-8") + b"\x00" + study_id.encode("utf-8")
    return crc32c(material) % BUCKETS


def deployment_rank(role: str, traffic_permille: int, bucket: int) -> int:
    """`MOS-REG-059`'s three-line rank, verbatim.

        if role == CANARY and bucket < traffic_permille:  2
        elif role == ACTIVE:                              1
        elif role == CANARY:                              0

    A `SHADOW` or `STANDBY` role never reaches here: `F4` has already excluded it, and a
    shadow deployment MUST NOT serve a clinical result (`MOS-REG-082`). It returns 0 for
    them so that a caller which somehow passes one in ranks it last rather than first.
    """
    if role == "CANARY" and bucket < traffic_permille:
        return 2
    if role == "ACTIVE":
        return 1
    return 0
