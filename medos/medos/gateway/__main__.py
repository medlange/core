# SPDX-License-Identifier: Apache-2.0
"""`python -m medos.gateway` -- run the Gateway with uvicorn.

Mirrors `medos.api`'s deployment shape: `create_app` is a FACTORY and there is
deliberately no module-level `app`, so two instances with different backends share nothing
(`MOS-REL-046`).

The PACS credential is read here and nowhere else in the process, by
`medos.gateway.backend.backend_from_env`, which `create_app` calls. MOS-DATA-005: "The
credential MUST be delivered to the Gateway process from a secret store at start-up."
"""

from __future__ import annotations

import argparse
import logging

from medos.gateway.app import DEFAULT_PORT, create_app

log = logging.getLogger("medos.gateway")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - a process entry point
    parser = argparse.ArgumentParser(prog="python -m medos.gateway")
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container-internal
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)

    import uvicorn

    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        # `medos.api.app.TraceContextMiddleware` already logs one JSON line per request
        # WITH the trace id; uvicorn's access log would print a second, trace-less copy.
        access_log=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
