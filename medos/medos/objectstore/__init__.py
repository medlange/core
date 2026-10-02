# SPDX-License-Identifier: Apache-2.0
"""The object store client.

NOT DEFINED BY CONTRACT.md §1, AND THAT IS REPORTED RATHER THAN ASSUMED
    CONTRACT.md §1 fixes the weeks 1-2 layout and §0 lists "no minio / s3" as deliberately
    absent from that slice.  Weeks 3-5 (`docs/spec/15-delivery.md` §15.2.4) moves native
    inference onto Triton, and chapter 2 `MOS-SVC-058` forbids baking model weights into
    a runner image -- which requires somewhere for weights to live.  CONTRACT.md §1's own
    rule for this case is "define it ONLY inside your own module and report it", so this
    package holds the client and nothing else: no bucket layout policy, no lifecycle, no
    PHI.  Chapter 12 owns the storage plane proper; when it lands, this is the module it
    absorbs or replaces.

    The ONLY object keys this package is used for today are model artifacts under
    `models/{model_id}/{version}/...` (see `medos.inference.tritond`).  No imaging data
    and no PHI passes through it.

Spec: MOS-SVC-058, MOS-OPS-070.
"""

from medos.objectstore.s3 import (
    ObjectNotFound,
    S3Client,
    S3Config,
    S3Error,
    sha256_hex,
)

__all__ = ["S3Client", "S3Config", "S3Error", "ObjectNotFound", "sha256_hex"]
