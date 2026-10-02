# SPDX-License-Identifier: Apache-2.0
"""Adapters: the seams the SDK plugs into a deployment.

An adapter is a Protocol plus the shipped drivers that implement it. The Protocols are
the SDK's surface; the drivers are how THIS repository speaks to the concrete systems the
compose stack runs (a DICOMweb PACS, a KServe v2 / Triton server, a Kafka or RabbitMQ
bus). A site with something else implements the Protocol against its own system and
passes that to `medos.sdk.pipeline.Pipeline` the same way.

The drivers import their transport LAZILY, inside the constructor, so
`pip install medos` -- the SDK without `[server]` -- never requires `requests`, a Kafka
client or anything else a driver speaks. Constructing a driver is the act of opting into
its dependencies.

Spec: the SDK pivot (platform became the SDK; adapters are its connection points).
"""

from __future__ import annotations

__all__ = ["DriverMissing"]


class DriverMissing(RuntimeError):
    """The transport a driver needs is not installed.

    Raised lazily at CONSTRUCTION, not at import, so `import medos.sdk.adapters` stays
    free of the driver's dependencies. The message names the extra that provides them.
    """
