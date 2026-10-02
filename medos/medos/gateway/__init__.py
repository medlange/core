# SPDX-License-Identifier: Apache-2.0
"""`medicalos-gateway` -- the DICOM Gateway. MOS-DATA-001, MOS-DATA-002.

    "The medical data plane consists of exactly these components: the **DICOM Gateway**,
    the **Ingest Controller**, the **Triage Engine**, and the **Selection Engine**. No
    other component MAY read from or write to a PACS."   -- MOS-DATA-001

    "No component other than the DICOM Gateway MAY hold, mount, receive or derive a PACS
    credential. This includes the control plane, workers, service containers (both
    `native` and `sealed`), the OHIF viewer, the evidence-plane exporter, developer
    tooling, and Airflow-style batch jobs."               -- MOS-DATA-002

WHY THIS IS `medos/medos/gateway/` AND NOT `medos/medos/dicomweb/gateway.py`
-----------------------------------------------------------------
CONTRACT.md section 1 lists `dicomweb/gateway.py` as "the ONLY holder of PACS credentials
in later releases", and in weeks 1-2 that file became `DicomWebGateway`: a CLIENT facade
the worker uses to pull a series and push a SEG. The thing this package contains is the
SERVER -- the proxy that facade now talks to. Two different objects, and the weeks 1-2
name went to the client.

Rather than rename `DicomWebGateway` (which appears in the worker, four test modules and
the frozen spike's lineage) the server got its own package and the client kept its name.
The credential moved with the server: `medos.dicomweb.gateway.GatewayConfig` still reads
`MEDOS_DICOMWEB_*`, but in the re-pointed deployment those variables now hold a GATEWAY
TOKEN and a Gateway URL, never a PACS credential and never a PACS address. The PACS
variables are `MEDOS_GATEWAY_PACS_*` and are read only by `medos.gateway.backend`.

Lazy imports below so that `medos.gateway.auth` or `.projection` can be imported by a
test without pulling FastAPI and `requests` into the process.
"""

from __future__ import annotations

from typing import Any

__all__ = ["create_app", "GatewayConfig", "DEFAULT_PORT"]


def __getattr__(name: str) -> Any:
    if name in ("create_app", "GatewayConfig", "DEFAULT_PORT"):
        from medos.gateway import app as _app

        return getattr(_app, name)
    raise AttributeError(name)
