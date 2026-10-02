# SPDX-License-Identifier: Apache-2.0
"""MOS-DATA-026's five metric families, and the label rule that comes with them.

    "The Gateway MUST expose: `medicalos_gateway_requests_total{operation,consumer_class,
    status}`, `medicalos_gateway_deid_duration_seconds{profile_version}`,
    `medicalos_gateway_deid_failures_total{reason}`, `medicalos_gateway_uidmap_lookups_
    total{result}` where `result` in {hit, minted}, and
    `medicalos_gateway_tenant_denials_total{reason}`. Label values MUST NOT contain a UID,
    a patient identifier or a description string (spine section 8)."

    -- docs/spec/03-medical-data-plane.md, MOS-DATA-026

WHY THIS IS FIFTY LINES AND NOT `prometheus_client`
----------------------------------------------------
Five counters and one histogram is less code than the dependency, and
`medos/deploy/compose/requirements.txt` states pinned versions for a deliberately small runtime
set. More to the point, the requirement's binding half is the LABEL RULE, and a registry
that validates label values against it is not something the library would do: `_label`
below raises on a value that looks like a UID, so "someone adds the study UID as a label
while debugging a cache-miss problem" fails a unit test instead of shipping a
high-cardinality PHI-adjacent metric to whatever scrapes it.

The three de-identification families are declared and are always zero, because this block
implements no de-identification (see `medos.gateway.app`'s module docstring). Declaring
them at zero rather than omitting them is the honest shape: a dashboard panel that reads
"no data" is indistinguishable from a broken exporter, while a flat zero next to a
non-zero request count says plainly that the stage is not running.
"""

from __future__ import annotations

import re
import threading

__all__ = ["Metrics", "LabelValueError"]

# A label value that looks like a DICOM UID, an MRN-ish identifier, or free text with
# spaces. Deliberately blunt: every legitimate label value in MOS-DATA-026 is a
# lower-snake word, an HTTP status, a small integer or a fixed enum member.
_UID_LIKE = re.compile(r"^\d+(\.\d+){3,}$")
_ALLOWED = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


class LabelValueError(ValueError):
    """MOS-DATA-026: "Label values MUST NOT contain a UID, a patient identifier or a
    description string"."""


def _label(value: object) -> str:
    text = str(value)
    if _UID_LIKE.match(text):
        raise LabelValueError(
            "a DICOM UID must not appear in a metric label (MOS-DATA-026)"
        )
    if not _ALLOWED.match(text):
        raise LabelValueError(
            f"label value {text!r} is not a short enum-shaped token; MOS-DATA-026 "
            "forbids description strings and identifiers in labels"
        )
    return text


class Metrics:
    """Process-local counters. One instance per app (CONTRACT.md section 11: no singleton).

    Thread-safe because the retrieval path releases its concurrency slot and records its
    counters from a streaming generator, which Starlette runs on a worker thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: dict[tuple[str, str, str], int] = {}
        self.tenant_denials: dict[str, int] = {}
        self.deid_failures: dict[str, int] = {}
        self.uidmap_lookups: dict[str, int] = {"hit": 0, "minted": 0}
        self.deid_duration_sum: dict[str, float] = {}
        self.deid_duration_count: dict[str, int] = {}

    # -- recording ---------------------------------------------------------------------
    def request(self, *, operation: str, consumer_class: str, status: int) -> None:
        key = (_label(operation), _label(consumer_class), _label(status))
        with self._lock:
            self.requests[key] = self.requests.get(key, 0) + 1

    def denial(self, *, reason: str) -> None:
        r = _label(reason)
        with self._lock:
            self.tenant_denials[r] = self.tenant_denials.get(r, 0) + 1

    def deid_failure(self, *, reason: str) -> None:
        r = _label(reason)
        with self._lock:
            self.deid_failures[r] = self.deid_failures.get(r, 0) + 1

    def uidmap(self, *, result: str) -> None:
        if result not in ("hit", "minted"):
            raise LabelValueError("uidmap result is exactly {hit, minted} (MOS-DATA-026)")
        with self._lock:
            self.uidmap_lookups[result] += 1

    def deid_duration(self, *, profile_version: int, seconds: float) -> None:
        v = _label(profile_version)
        with self._lock:
            self.deid_duration_sum[v] = self.deid_duration_sum.get(v, 0.0) + seconds
            self.deid_duration_count[v] = self.deid_duration_count.get(v, 0) + 1

    # -- exposition --------------------------------------------------------------------
    def render(self) -> str:
        """Prometheus text exposition format 0.0.4."""
        out: list[str] = []
        with self._lock:
            out.append("# HELP medicalos_gateway_requests_total DICOMweb requests served.")
            out.append("# TYPE medicalos_gateway_requests_total counter")
            for (op, cls, status), n in sorted(self.requests.items()):
                out.append(
                    f'medicalos_gateway_requests_total{{operation="{op}",'
                    f'consumer_class="{cls}",status="{status}"}} {n}'
                )

            out.append(
                "# HELP medicalos_gateway_tenant_denials_total "
                "Requests refused by the tenancy or scope check."
            )
            out.append("# TYPE medicalos_gateway_tenant_denials_total counter")
            for reason, n in sorted(self.tenant_denials.items()):
                out.append(
                    f'medicalos_gateway_tenant_denials_total{{reason="{reason}"}} {n}'
                )

            out.append(
                "# HELP medicalos_gateway_deid_failures_total "
                "De-identification failures. Always zero until de-identification ships."
            )
            out.append("# TYPE medicalos_gateway_deid_failures_total counter")
            if not self.deid_failures:
                out.append('medicalos_gateway_deid_failures_total{reason="none"} 0')
            for reason, n in sorted(self.deid_failures.items()):
                out.append(
                    f'medicalos_gateway_deid_failures_total{{reason="{reason}"}} {n}'
                )

            out.append(
                "# HELP medicalos_gateway_uidmap_lookups_total "
                "deid_uid_map lookups. Always zero until de-identification ships."
            )
            out.append("# TYPE medicalos_gateway_uidmap_lookups_total counter")
            for result in ("hit", "minted"):
                out.append(
                    f'medicalos_gateway_uidmap_lookups_total{{result="{result}"}} '
                    f"{self.uidmap_lookups[result]}"
                )

            out.append(
                "# HELP medicalos_gateway_deid_duration_seconds "
                "Time spent de-identifying one response."
            )
            out.append("# TYPE medicalos_gateway_deid_duration_seconds summary")
            versions = set(self.deid_duration_sum) | {"0"}
            for v in sorted(versions):
                out.append(
                    f'medicalos_gateway_deid_duration_seconds_sum{{profile_version="{v}"}} '
                    f"{self.deid_duration_sum.get(v, 0.0)}"
                )
                out.append(
                    f'medicalos_gateway_deid_duration_seconds_count'
                    f'{{profile_version="{v}"}} {self.deid_duration_count.get(v, 0)}'
                )
        return "\n".join(out) + "\n"
