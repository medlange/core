# SPDX-License-Identifier: Apache-2.0
"""DICOMweb access: the transport half of the medical data plane.

CONTRACT.md §1 gives this package two modules and one job each:

  * `client.py`  -- `DicomWebClient`: QIDO-RS, WADO-RS, STOW-RS. Speaks HTTP and DICOM
    media types and nothing else. It has no opinion about which series is interesting.
  * `gateway.py` -- `DicomWebGateway`: the single choke point for PACS access, and in
    later releases the ONLY holder of PACS credentials.

Nothing else in `medos` may construct a `requests.Session` at a PACS, and nothing
outside this package may read the credential environment variables. `medos.core` is pure
(CONTRACT.md §1) and a capability MUST NOT touch the network (CONTRACT.md §6), so this
package is the whole of MedicalOS's outward network surface in this slice.

Chapter 3's public Gateway routes carry a `{t}` tenant segment
(`/dicomweb/{t}/studies`, MOS-DATA-007). This slice has no tenancy (CONTRACT.md §0), so
the segment is absent and `base_url` names the DICOMweb root directly. Weeks 3-5 adds the
segment and MOS-DATA-009's "the effective tenant is derived from the authenticated
principal, never from the URL"; the shape of `DicomWebGateway` is chosen so that lands as
a change to this package alone.

Spec: MOS-DATA-007, MOS-DATA-023, MOS-DATA-058, MOS-IMG-081, MOS-IMG-082, MOS-IMG-084,
MOS-IMG-085, MOS-IMG-152.
"""

from medos.dicomweb.client import (
    DICOM_JSON,
    MULTIPART_DICOM,
    DicomWebClient,
    FailedInstance,
    StowResult,
    parse_multipart_related,
    sop_uids_from_qido,
)
from medos.dicomweb.gateway import (
    DicomWebGateway,
    FetchedSeries,
    GatewayConfig,
    PacsCredentials,
)

__all__ = [
    "DicomWebClient",
    "StowResult",
    "FailedInstance",
    "parse_multipart_related",
    "sop_uids_from_qido",
    "MULTIPART_DICOM",
    "DICOM_JSON",
    "DicomWebGateway",
    "GatewayConfig",
    "PacsCredentials",
    "FetchedSeries",
]
