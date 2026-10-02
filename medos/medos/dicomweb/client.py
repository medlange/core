# SPDX-License-Identifier: Apache-2.0
"""`DicomWebClient` -- QIDO-RS, WADO-RS and STOW-RS over HTTP.

CONTRACT.md §1: "client.py  # DicomWebClient: QIDO/WADO/STOW". The QIDO and STOW halves
are LIFTED from `spikes/week0/ingest.py` per CONTRACT.md §2 -- the multipart assembly,
the `FailedSOPSequence` rule and the resource-addressed QIDO form are that file's bodies,
moved rather than rewritten. WADO-RS is new: the spike only ever pushed, and the weeks
1-2 pipeline begins with a pull (`docs/spec/15-delivery.md` §15.2.3, "DICOMweb pull").

WHY THIS IS HAND-ROLLED AND NOT A DICOMweb SDK
    Inherited from the spike, and the reason still holds: this layer is partly a test *of*
    the wire format. A library that silently normalises a malformed multipart body, or
    that treats HTTP 200 as success and drops `FailedSOPSequence`, would hide exactly the
    failure MOS-IMG-084 exists to catch. It also keeps the dependency budget at
    `requests` + `pydicom`, which is what `spikes/week0/requirements.txt` pins.

WHY STOW-RS AND NOT C-STORE
    Chapter 13 (MOS-OPS-005) puts a DICOMweb gateway in front of the PACS and gives
    workers no DIMSE route to it. Every write MedicalOS performs is a STOW-RS.

WHAT "SUCCESS" MEANS HERE
    MOS-IMG-084: a STOW-RS succeeded only when the status is 200 **and**
    `FailedSOPSequence (0008,1198)` is empty. Orthanc answers 200 for a partial store.
    `StowResult.ok` implements that rule; the HTTP status alone is never evidence.
    MOS-IMG-152 then requires **set** equality of `SOPInstanceUID`s on the QIDO re-read,
    not count equality -- a count check passes when one instance is dropped and an
    unrelated one is already present.

MEMORY
    MOS-DATA-023 forbids buffering a whole series. `iter_series_instances` decodes the
    multipart response incrementally: the in-flight buffer holds one instance (~0.5 MB for
    512x512x16-bit CT) plus one socket chunk, never the 400-instance body. The
    non-streaming `parse_multipart_related` exists for single-instance responses and for
    tests, and says so.

PHI
    CONTRACT.md §11: never log a PHI value. A QIDO-RS success body is `application/
    dicom+json` full of PatientName, PatientID and dates, so this module NEVER echoes a
    2xx body into an exception message -- only its media type and length. An error body
    (status >= 4xx) is a status report rather than a data payload and IS excerpted,
    truncated, because without it a misconfigured origin is undebuggable. Nothing here
    logs; it raises, and the caller decides.

Spec: MOS-DATA-007, MOS-DATA-021, MOS-DATA-023, MOS-DATA-058, MOS-IMG-082, MOS-IMG-084,
MOS-IMG-085, MOS-IMG-096, MOS-IMG-152, MOS-OPS-005.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    raise SystemExit(
        f"medos.dicomweb requires requests ({exc}). pip install -e .[dev]"
    ) from exc

from medos.core.errors import SeriesSelectionError, TransportFailure

__all__ = [
    "DicomWebClient",
    "StowResult",
    "FailedInstance",
    "MultipartPart",
    "parse_multipart_related",
    "sop_uids_from_qido",
    "MULTIPART_DICOM",
    "DICOM_JSON",
    "TRANSPORT_REASON_CODES",
]


# --------------------------------------------------------------------------------------
# Media types and DICOM JSON keys (PS3.18 §10.5, §8.7)
# --------------------------------------------------------------------------------------
MULTIPART_DICOM = "application/dicom"
DICOM_JSON = "application/dicom+json"

# STOW-RS response keys.
TAG_REFERENCED_SOP_SEQUENCE = "00081199"
TAG_FAILED_SOP_SEQUENCE = "00081198"
TAG_REFERENCED_SOP_INSTANCE_UID = "00081155"
TAG_REFERENCED_SOP_CLASS_UID = "00081150"
TAG_FAILURE_REASON = "00081197"
# QIDO-RS result keys.
TAG_SOP_INSTANCE_UID = "00080018"
TAG_SERIES_INSTANCE_UID = "0020000E"
TAG_STUDY_INSTANCE_UID = "0020000D"
TAG_MODALITY = "00080060"
TAG_NUMBER_OF_SERIES_RELATED_INSTANCES = "00201209"

# CONTRACT.md §9 fixes the problem `class` enum but not the reason codes inside it.
# These are this module's; they are the discriminator a caller switches on, so they are
# declared as a closed set here rather than spelled inline at each raise site.
TRANSPORT_REASON_CODES: frozenset[str] = frozenset(
    {
        "dicomweb_unreachable",  # TCP/TLS/DNS/timeout -- no HTTP answer at all
        "dicomweb_root_not_found",  # 404 on the DICOMweb root: wrong path or no plugin
        "dicomweb_unauthorized",  # 401/403 -- credentials wrong or absent
        "dicomweb_http_error",  # any other >= 400
        "dicomweb_bad_response",  # 2xx whose body is not the promised media type
        "dicomweb_stow_incomplete",  # MOS-IMG-084: 200 but a non-empty FailedSOPSequence
        "dicomweb_series_incomplete",  # MOS-IMG-152: set inequality after a retrieve
    }
)


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FailedInstance:
    """One row of `FailedSOPSequence (0008,1198)`.

    `failure_reason` is the DICOM status code the origin returned (PS3.18 Table
    I.2-1), kept as an int so a caller can distinguish "already exists" from "out of
    resources" without string matching.
    """

    sop_instance_uid: str
    sop_class_uid: str
    failure_reason: int


@dataclass(frozen=True)
class StowResult:
    """The outcome of one STOW-RS request.

    `ok` is MOS-IMG-084 and nothing else: status 200 AND an empty `FailedSOPSequence`.
    The HTTP status is retained separately because a caller writing a report needs to be
    able to say *which* of the two conditions failed.
    """

    http_status: int
    referenced_sop_instance_uids: tuple[str, ...]
    failed: tuple[FailedInstance, ...]
    bytes_sent: int
    seconds: float

    @property
    def ok(self) -> bool:
        """MOS-IMG-084: 200 and an empty FailedSOPSequence. Not `status == 200`."""
        return self.http_status == 200 and not self.failed


@dataclass(frozen=True)
class MultipartPart:
    """One part of a `multipart/related` response."""

    headers: Mapping[str, str]
    content: bytes

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


# --------------------------------------------------------------------------------------
# Multipart
# --------------------------------------------------------------------------------------
def _boundary_from_content_type(content_type: str) -> bytes:
    """Extract the multipart boundary, honouring the quoted form.

    RFC 2046 §5.1.1 allows `boundary="..."`; Orthanc emits the unquoted form and other
    origins emit the quoted one, so both must work or the client is origin-specific.
    """
    for token in content_type.split(";"):
        token = token.strip()
        if token.lower().startswith("boundary="):
            value = token[len("boundary=") :].strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            if value:
                return value.encode("ascii", errors="strict")
    raise TransportFailure(
        "dicomweb_bad_response",
        {"content_type": content_type},
        "multipart/related response carries no boundary parameter",
    )


def _split_part(raw: bytes) -> MultipartPart:
    """Split one raw multipart segment into headers and body.

    `raw` is everything between the CRLF that follows a boundary line and the CRLF that
    precedes the next one.
    """
    if raw.startswith(b"\r\n"):
        raw = raw[2:]
    sep = raw.find(b"\r\n\r\n")
    if sep < 0:
        # A part with no header block is malformed; treating it as an all-body part would
        # hand the caller a DICOM file with the headers glued to its preamble.
        raise TransportFailure(
            "dicomweb_bad_response",
            {"part_bytes": len(raw)},
            "multipart part has no CRLFCRLF header terminator",
        )
    headers: dict[str, str] = {}
    for line in raw[:sep].split(b"\r\n"):
        if not line:
            continue
        name, _, value = line.partition(b":")
        headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return MultipartPart(headers=headers, content=raw[sep + 4 :])


class _MultipartStreamDecoder:
    """Incremental `multipart/related` decoder.

    Holds at most one part plus one socket chunk, which is what makes a 400-instance
    WADO-RS series retrieve bounded in memory (MOS-DATA-023: the transport MUST NOT
    buffer a whole series).

    The synthetic leading CRLF makes one delimiter (`CRLF--boundary`) match both the
    first boundary -- which RFC 2046 allows to appear with no preceding CRLF -- and every
    later one, so there is no special case for part zero.
    """

    def __init__(self, boundary: bytes) -> None:
        self._delim = b"\r\n--" + boundary
        self._buf = bytearray(b"\r\n")
        # Where the next delimiter search starts. The buffer holds the part currently
        # being assembled, so it MUST NOT be trimmed -- an earlier version of this class
        # discarded the head of the buffer between chunks to "bound memory" and shredded
        # every instance larger than one socket chunk. The bound comes from consuming a
        # part as soon as its closing delimiter arrives, not from throwing bytes away.
        self._scan = 0
        self._seen_first_boundary = False
        self.closed = False

    def feed(self, chunk: bytes) -> Iterator[MultipartPart]:
        self._buf += chunk
        yield from self._drain()

    def _drain(self) -> Iterator[MultipartPart]:
        while not self.closed:
            idx = self._buf.find(self._delim, self._scan)
            if idx < 0:
                # A delimiter can straddle two chunks, so resume the next search far
                # enough back to catch one that is partly present.
                self._scan = max(0, len(self._buf) - len(self._delim) + 1)
                return
            end = idx + len(self._delim)
            if len(self._buf) < end + 2:
                # Need the two bytes that say "another part" (CRLF) vs "--" (the end).
                self._scan = idx
                return
            tail = bytes(self._buf[end : end + 2])
            segment = bytes(self._buf[:idx])
            del self._buf[:end]
            self._scan = 0
            if self._seen_first_boundary:
                yield _split_part(segment)
            self._seen_first_boundary = True
            if tail == b"--":
                self.closed = True
                return

    def finish(self) -> None:
        if not self.closed:
            raise TransportFailure(
                "dicomweb_bad_response",
                {"pending_bytes": len(self._buf)},
                "multipart/related body ended without its closing boundary",
            )


def parse_multipart_related(body: bytes, content_type: str) -> list[MultipartPart]:
    """Non-streaming parse. For single-instance responses and for tests.

    Deliberately separate from `_MultipartStreamDecoder` at the API level and shared with
    it at the implementation level: a series retrieve must NOT reach for this function,
    and a function that materialises the whole body should be named so that a reviewer
    notices when it does.
    """
    decoder = _MultipartStreamDecoder(_boundary_from_content_type(content_type))
    parts = list(decoder.feed(body))
    decoder.finish()
    return parts


def sop_uids_from_qido(rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """Project `SOPInstanceUID` out of QIDO-RS instance rows.

    A **set**, because MOS-IMG-152 requires set equality and a list invites a caller to
    compare lengths instead.
    """
    out: set[str] = set()
    for row in rows:
        value = (row.get(TAG_SOP_INSTANCE_UID) or {}).get("Value") or []
        if value:
            out.add(str(value[0]))
    return out


def _first(row: Mapping[str, Any], tag: str, default: str = "") -> str:
    value = (row.get(tag) or {}).get("Value") or []
    if not value:
        return default
    item = value[0]
    return str(item) if not isinstance(item, dict) else str(item.get("Alphabetic", default))


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------
class DicomWebClient:
    """Minimal QIDO-RS / WADO-RS / STOW-RS client.

    Construct it through `medos.dicomweb.gateway.DicomWebGateway` in application code:
    CONTRACT.md §1 makes the gateway the only credential holder in later releases, and a
    second construction site is how that stops being true.

    Every failure raises `medos.core.errors.TransportFailure` (CONTRACT.md §9
    `class: "transport_failure"`, retryable, HTTP 502). A PACS that is down is not a bad
    request from the caller and is not a clinical rejection, and the three MUST stay
    distinguishable in metrics (chapter 5 `MOS-EXEC-001`).
    """

    def __init__(
        self,
        base_url: str,
        *,
        user: str | None = None,
        password: str | None = None,
        bearer_token: str | None = None,
        timeout_s: float = 300.0,
        verify_tls: bool = True,
        session: requests.Session | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required (the DICOMweb root, e.g. .../dicom-web)")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = float(timeout_s)
        self.session = session or requests.Session()
        self.session.verify = verify_tls
        if bearer_token:
            self.session.headers["Authorization"] = f"Bearer {bearer_token}"
        elif user is not None:
            self.session.auth = (user, password or "")

    def __repr__(self) -> str:
        # No credentials in a repr: a repr reaches logs, tracebacks and error reports.
        return f"DicomWebClient(base_url={self.base_url!r})"

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> DicomWebClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- HTTP ------------------------------------------------------------------------
    def _get(
        self,
        url: str,
        *,
        accept: str,
        params: Mapping[str, Any] | None = None,
        stream: bool = False,
        extra_headers: Mapping[str, str] | None = None,
    ) -> requests.Response:
        headers = {"Accept": accept}
        if extra_headers:
            headers.update(extra_headers)
        try:
            resp = self.session.get(
                url,
                headers=headers,
                params=dict(params or {}),
                timeout=self.timeout_s,
                stream=stream,
            )
        except requests.RequestException as exc:
            raise TransportFailure(
                "dicomweb_unreachable",
                {"url": url, "method": "GET", "error": type(exc).__name__},
                f"cannot reach the DICOMweb origin: {exc}",
            ) from exc
        self._raise_for_status(resp, url, "GET")
        return resp

    def _raise_for_status(self, resp: requests.Response, url: str, method: str) -> None:
        if resp.status_code < 400:
            return
        # An error body is a status report, not a data payload, so excerpting it does not
        # violate CONTRACT.md §11. A 2xx body is never excerpted anywhere in this module.
        excerpt = ""
        try:
            excerpt = resp.text[:400]
        except Exception:  # noqa: BLE001 - an undecodable error body must not mask the error
            excerpt = "<undecodable>"
        detail = {
            "url": url,
            "method": method,
            "http_status": resp.status_code,
            "body_excerpt": excerpt,
        }
        if resp.status_code == 404:
            # A 404 is two different things and conflating them hands a radiologist a
            # system error for a clinical outcome -- the exact conflation the RFC 9457
            # `class` enum exists to prevent (spec 15.2.4), and why `rejection-distinct`
            # is a Tier A release gate.
            #
            #   * the DICOMweb ROOT is missing (wrong path, plugin not loaded)
            #        -> a deployment fault. TransportFailure -> FAILED. Retryable once
            #           somebody fixes the configuration.
            #   * the root is fine and the STUDY is not held
            #        -> a study with, trivially, no eligible series. ClinicalRejection
            #           -> REJECTED, reason `no_eligible_series`, never retryable.
            #
            # The Gateway already draws this line: MOS-DATA-013 makes it answer a missing
            # study with `STUDY_NOT_FOUND` / `client_error`, reserving `BACKEND_REFUSED` /
            # `transport_failure` for a backend that is genuinely unwell. This client used
            # to throw that distinction away and call every 404 a missing root, which is
            # the regression `rejection-distinct` caught.
            upstream_code = ""
            try:
                body = resp.json()
                if isinstance(body, dict):
                    upstream_code = str(body.get("code") or "")
            except Exception:  # noqa: BLE001 - a non-JSON 404 is the root case below
                upstream_code = ""
            if upstream_code == "STUDY_NOT_FOUND":
                raise SeriesSelectionError(
                    "no_eligible_series",
                    detail | {"upstream_code": upstream_code},
                    f"{method} {url} -> 404: the archive does not hold this study, so it "
                    "has no eligible series.",
                )
            raise TransportFailure(
                "dicomweb_root_not_found",
                detail,
                f"{method} {url} -> 404: reachable, but nothing is served there. "
                "Check the DICOMweb root path and that the plugin is loaded.",
            )
        if resp.status_code in (401, 403):
            raise TransportFailure(
                "dicomweb_unauthorized",
                detail,
                f"{method} {url} -> {resp.status_code}: the origin refused these "
                "credentials.",
            )
        raise TransportFailure(
            "dicomweb_http_error", detail, f"{method} {url} -> {resp.status_code}"
        )

    def _json(self, resp: requests.Response, url: str) -> list[dict[str, Any]]:
        if resp.status_code == 204 or not resp.content:
            return []  # PS3.18 §8.3.4.3: 204 means "no matches", not an error.
        try:
            rows = resp.json()
        except ValueError as exc:
            # Deliberately no body excerpt: a 2xx dicom+json body carries PHI.
            raise TransportFailure(
                "dicomweb_bad_response",
                {
                    "url": url,
                    "content_type": resp.headers.get("Content-Type", ""),
                    "content_length": len(resp.content),
                },
                f"expected {DICOM_JSON}; the origin returned an unparseable body",
            ) from exc
        if isinstance(rows, dict):
            return [rows]
        return list(rows) if isinstance(rows, list) else []

    # -- preflight -------------------------------------------------------------------
    def preflight(self) -> None:
        """Prove the DICOMweb root IS a DICOMweb root before sending megabytes at it.

        Lifted from `spikes/week0/ingest.py`. A missing plugin answers 404 on every STOW
        as well, but by then the error is buried in a multipart response and reads like a
        data problem instead of a configuration problem.

        ANSWERING IS NOT BEING THE PACS. This used to check the status code and throw the
        response away, and `available()` -- which the test suite and the readiness probes
        both use -- returned True for anything that did not 4xx. MOS-OPS-005 puts the
        viewer, the API and the PACS behind ONE origin, and that origin is an nginx whose
        SPA fallback answers `200 text/html` for every path it does not recognise. So a
        `MEDOS_DICOMWEB_URL` pointing one path segment wrong reported a healthy PACS and
        then failed deep inside a retrieve. Measured on the development stack:

            GET http://127.0.0.1:3000/dicom-web/studies -> 200 text/html (the OHIF index)
            DicomWebGateway.available()                 -> True

        A wrong root is a configuration failure and has to be named as one at preflight,
        which is the whole reason this method exists.
        """
        url = f"{self.base_url}/studies"
        resp = self._get(url, accept=DICOM_JSON, params={"limit": "1"})
        if resp.status_code == 204 or not resp.content:
            return  # PS3.18 §8.3.4.3: an empty archive is a working one.
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        detail = {
            "url": url,
            "http_status": resp.status_code,
            "content_type": content_type,
        }
        if "json" not in content_type:
            raise TransportFailure(
                "dicomweb_bad_response",
                detail,
                f"GET {url} -> {resp.status_code} {content_type or '(no content-type)'}: "
                f"the origin answered, but not with {DICOM_JSON}. This is a wrong "
                "DICOMweb root or a missing plugin, not an empty archive.",
            )
        try:
            rows = resp.json()
        except ValueError as exc:
            # No body excerpt: a 2xx dicom+json body carries PHI (CONTRACT.md §11).
            raise TransportFailure(
                "dicomweb_bad_response",
                detail,
                f"GET {url} -> {resp.status_code}: unparseable {DICOM_JSON} body",
            ) from exc
        if not isinstance(rows, list):
            raise TransportFailure(
                "dicomweb_bad_response",
                detail | {"json_type": type(rows).__name__},
                f"GET {url} -> {resp.status_code}: QIDO-RS returns an array; this origin "
                f"returned a {type(rows).__name__}.",
            )

    # -- QIDO-RS ---------------------------------------------------------------------
    def qido_studies(self, **filters: Any) -> list[dict[str, Any]]:
        """Study-level QIDO-RS. `filters` are DICOM keywords, e.g. `StudyInstanceUID=...`."""
        url = f"{self.base_url}/studies"
        return self._json(self._get(url, accept=DICOM_JSON, params=filters), url)

    def qido_series(self, study_instance_uid: str, **filters: Any) -> list[dict[str, Any]]:
        """Series-level QIDO-RS within one study."""
        url = f"{self.base_url}/studies/{study_instance_uid}/series"
        return self._json(self._get(url, accept=DICOM_JSON, params=filters), url)

    def qido_instances(
        self, study_instance_uid: str, series_instance_uid: str, **filters: Any
    ) -> list[dict[str, Any]]:
        """Instance-level QIDO-RS for one series.

        Uses the resource-addressed form rather than a flat
        `/instances?SeriesInstanceUID=` query: the flat form depends on the origin
        supporting relational queries, which PS3.18 makes optional. (Lifted from the
        spike, where the same reasoning is recorded.)
        """
        url = (
            f"{self.base_url}/studies/{study_instance_uid}"
            f"/series/{series_instance_uid}/instances"
        )
        return self._json(self._get(url, accept=DICOM_JSON, params=filters), url)

    def instance_uids(self, study_instance_uid: str, series_instance_uid: str) -> set[str]:
        """The set of `SOPInstanceUID`s the origin holds for one series.

        This is the read MOS-IMG-082 ("completeness MUST be determined by comparing the
        set of SOPInstanceUIDs") and MOS-IMG-081 (reconciliation) are written against.
        """
        return sop_uids_from_qido(
            self.qido_instances(study_instance_uid, series_instance_uid)
        )

    # -- WADO-RS ---------------------------------------------------------------------
    def wado_study_metadata(self, study_instance_uid: str) -> list[dict[str, Any]]:
        """Study metadata as `application/dicom+json`, no pixel data.

        MOS-DATA-058 makes this the ONLY retrieve triage may issue: "Triage MUST read
        metadata only ... and MUST NOT issue any WADO-RS instance, frame or rendered
        request." A selection step that calls `iter_series_instances` instead has already
        pulled the pixels it was supposed to decide about.
        """
        url = f"{self.base_url}/studies/{study_instance_uid}/metadata"
        return self._json(self._get(url, accept=DICOM_JSON), url)

    def wado_series_metadata(
        self, study_instance_uid: str, series_instance_uid: str
    ) -> list[dict[str, Any]]:
        """Series metadata, the per-series fallback MOS-DATA-058 names for large studies."""
        url = (
            f"{self.base_url}/studies/{study_instance_uid}"
            f"/series/{series_instance_uid}/metadata"
        )
        return self._json(self._get(url, accept=DICOM_JSON), url)

    def wado_instance(
        self, study_instance_uid: str, series_instance_uid: str, sop_instance_uid: str
    ) -> bytes:
        """Retrieve one instance as DICOM Part-10 bytes.

        `transfer-syntax=*` asks the origin to transcode nothing: MOS-IMG-085 and the UID
        derivation of chapter 4 both depend on the bytes we read being the bytes the
        archive holds. An origin that silently decompresses changes the pixel digest.
        """
        url = (
            f"{self.base_url}/studies/{study_instance_uid}"
            f"/series/{series_instance_uid}/instances/{sop_instance_uid}"
        )
        resp = self._get(
            url, accept=f'multipart/related; type="{MULTIPART_DICOM}"; transfer-syntax=*'
        )
        content_type = resp.headers.get("Content-Type", "")
        if "multipart/related" not in content_type.lower():
            # Some origins answer a single-instance retrieve with the bare object.
            return resp.content
        parts = parse_multipart_related(resp.content, content_type)
        if len(parts) != 1:
            raise TransportFailure(
                "dicomweb_bad_response",
                {"url": url, "n_parts": len(parts)},
                f"instance retrieve returned {len(parts)} parts; expected exactly 1",
            )
        return parts[0].content

    def iter_series_instances(
        self,
        study_instance_uid: str,
        series_instance_uid: str,
        *,
        chunk_bytes: int = 1 << 20,
    ) -> Iterator[MultipartPart]:
        """Stream a whole series, one instance at a time.

        MOS-DATA-023 forbids buffering a whole series; this decodes incrementally, so
        peak memory is one instance plus one socket chunk regardless of series length.
        The 400-slice retrieve of `docs/spec/15-delivery.md` §15.2.3 is the case this
        exists for.

        The caller is responsible for consuming the iterator -- the HTTP response stays
        open until it is exhausted or closed.
        """
        url = f"{self.base_url}/studies/{study_instance_uid}/series/{series_instance_uid}"
        resp = self._get(
            url,
            accept=f'multipart/related; type="{MULTIPART_DICOM}"; transfer-syntax=*',
            stream=True,
        )
        content_type = resp.headers.get("Content-Type", "")
        if "multipart/related" not in content_type.lower():
            resp.close()
            raise TransportFailure(
                "dicomweb_bad_response",
                {"url": url, "content_type": content_type},
                "series retrieve did not return multipart/related",
            )
        decoder = _MultipartStreamDecoder(_boundary_from_content_type(content_type))
        try:
            for chunk in resp.iter_content(chunk_size=chunk_bytes):
                if not chunk:
                    continue
                yield from decoder.feed(chunk)
            decoder.finish()
        finally:
            resp.close()

    def retrieve_series_to_dir(
        self,
        study_instance_uid: str,
        series_instance_uid: str,
        dest_dir: Path,
        *,
        expected_sop_instance_uids: Sequence[str] | None = None,
    ) -> list[Path]:
        """Pull one series to disk and return the files, ordered by arrival.

        Files are named by `SOPInstanceUID` and nothing else. A filename built from
        PatientID or a description would put PHI on the worker's filesystem and in every
        stack trace that names the path (CONTRACT.md §11); the UID is the only identifier
        this layer is allowed to spell.

        Instances are written with `pydicom` never touched -- the bytes go straight from
        the socket to the file, so what is on disk is byte-identical to what the archive
        holds, which is what the pixel digest of MOS-IMG-063 is computed over.

        When `expected_sop_instance_uids` is given, the arrival set is checked against it
        as a **set** (MOS-IMG-152), and a mismatch raises rather than handing the volume
        builder a short series it would happily build a wrong-extent volume from.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        seen: list[str] = []
        for index, part in enumerate(
            self.iter_series_instances(study_instance_uid, series_instance_uid)
        ):
            sop_uid = _sop_uid_from_part(part, fallback=f"part{index:05d}")
            seen.append(sop_uid)
            path = dest_dir / f"{sop_uid}.dcm"
            path.write_bytes(part.content)
            written.append(path)

        if expected_sop_instance_uids is not None:
            expected = set(expected_sop_instance_uids)
            got = set(seen)
            if expected != got:
                raise TransportFailure(
                    "dicomweb_series_incomplete",
                    {
                        "study_instance_uid": study_instance_uid,
                        "series_instance_uid": series_instance_uid,
                        "n_expected": len(expected),
                        "n_retrieved": len(got),
                        "missing_sop_instance_uids": sorted(expected - got)[:20],
                        "unexpected_sop_instance_uids": sorted(got - expected)[:20],
                    },
                    "WADO-RS returned a different instance SET than QIDO-RS advertised "
                    "(MOS-IMG-152 compares sets, not counts)",
                )
        if len(set(seen)) != len(seen):
            # Two parts with the same UID means one overwrote the other on disk.
            raise TransportFailure(
                "dicomweb_bad_response",
                {
                    "series_instance_uid": series_instance_uid,
                    "n_parts": len(seen),
                    "n_distinct": len(set(seen)),
                },
                "the origin returned duplicate SOPInstanceUIDs in one series retrieve",
            )
        return written

    # -- STOW-RS ---------------------------------------------------------------------
    def stow(
        self,
        payloads: Sequence[tuple[str, bytes]],
        *,
        study_instance_uid: str | None = None,
    ) -> StowResult:
        """POST one `multipart/related` batch of Part-10 objects.

        Lifted from `spikes/week0/ingest.py`. The body is assembled by hand because
        `requests`' own multipart encoder emits `multipart/form-data` with
        `Content-Disposition` parts, which a DICOMweb origin is entitled to reject
        outright.

        `study_instance_uid` selects MOS-DATA-007's `POST /studies/{st}` form, which is
        what the chapter 4 writer uses: a derived SEG or SR belongs to the study it was
        computed from, and posting it to the study root lets an origin file it anywhere.

        Returns a `StowResult`; it does NOT raise on a partial store, because MOS-IMG-084
        makes that a result the caller must inspect and report, not an exception to
        swallow. It DOES raise on a transport failure and on a non-JSON body, because
        without `FailedSOPSequence` there is no way to know what happened.
        """
        if not payloads:
            raise ValueError("stow() called with no payloads")

        # NOT a DICOM UID. This is a MIME multipart boundary (RFC 2046) and is REQUIRED to
        # be unpredictable so it cannot collide with the payload bytes. MOS-IMG-062's ban
        # on uuid4 applies to the DICOM *writing* path -- SOP/Series/FrameOfReference UIDs
        # -- and this function writes no DICOM identifier at all. A repository-wide grep
        # for `uuid4` (the CI check MOS-IMG-069 asks for) should land here, read this, and
        # move on. Derived UIDs are `medos.core.uids.derive_uid` and nothing else.
        boundary = f"medicalos-{uuid.uuid4().hex}"
        b_boundary = boundary.encode("ascii")
        parts: list[bytes] = []
        for _uid, blob in payloads:
            parts.append(
                b"--" + b_boundary + b"\r\n"
                b"Content-Type: " + MULTIPART_DICOM.encode("ascii") + b"\r\n"
                b"Content-Length: " + str(len(blob)).encode("ascii") + b"\r\n"
                b"\r\n" + blob + b"\r\n"
            )
        parts.append(b"--" + b_boundary + b"--\r\n")
        body = b"".join(parts)

        url = f"{self.base_url}/studies"
        if study_instance_uid:
            url = f"{url}/{study_instance_uid}"
        headers = {
            "Content-Type": (
                f'multipart/related; type="{MULTIPART_DICOM}"; boundary={boundary}'
            ),
            "Accept": DICOM_JSON,
        }
        started = time.monotonic()
        try:
            resp = self.session.post(
                url, data=body, headers=headers, timeout=self.timeout_s
            )
        except requests.RequestException as exc:
            raise TransportFailure(
                "dicomweb_unreachable",
                {"url": url, "method": "POST", "error": type(exc).__name__},
                f"STOW-RS POST failed at the transport layer: {exc}",
            ) from exc
        elapsed = time.monotonic() - started

        # A STOW-RS can legitimately answer 202 Accepted (partial) or 409 Conflict (all
        # failed) with a body that names WHICH instances failed. Raising on status alone
        # would discard the FailedSOPSequence that MOS-IMG-084 requires us to read, so
        # only a status with no usable body is an exception.
        if resp.status_code >= 400 and not resp.content:
            self._raise_for_status(resp, url, "POST")

        parsed: dict[str, Any] = {}
        if resp.content:
            try:
                parsed = resp.json()
            except ValueError as exc:
                # An origin answering dicom+xml is conformant; we just cannot read the
                # FailedSOPSequence out of it, and MOS-IMG-084 requires that we can.
                raise TransportFailure(
                    "dicomweb_bad_response",
                    {
                        "url": url,
                        "http_status": resp.status_code,
                        "content_type": resp.headers.get("Content-Type", ""),
                        "content_length": len(resp.content),
                    },
                    f"STOW-RS returned a non-JSON body; MOS-IMG-084 needs "
                    f"FailedSOPSequence and Accept: {DICOM_JSON} was already requested",
                ) from exc

        referenced = tuple(
            _first(item, TAG_REFERENCED_SOP_INSTANCE_UID)
            for item in (parsed.get(TAG_REFERENCED_SOP_SEQUENCE) or {}).get("Value") or []
        )
        failed = tuple(
            FailedInstance(
                sop_instance_uid=_first(item, TAG_REFERENCED_SOP_INSTANCE_UID),
                sop_class_uid=_first(item, TAG_REFERENCED_SOP_CLASS_UID),
                failure_reason=int(
                    ((item.get(TAG_FAILURE_REASON) or {}).get("Value") or [0])[0]
                ),
            )
            for item in (parsed.get(TAG_FAILED_SOP_SEQUENCE) or {}).get("Value") or []
        )
        return StowResult(
            http_status=resp.status_code,
            referenced_sop_instance_uids=referenced,
            failed=failed,
            bytes_sent=len(body),
            seconds=elapsed,
        )

    def stow_files(
        self,
        paths: Sequence[Path],
        *,
        study_instance_uid: str | None = None,
        batch_size: int = 20,
    ) -> list[StowResult]:
        """STOW a list of Part-10 files in batches.

        20 instances per request keeps each multipart body around 5 MB for 512x512x16-bit
        CT (the spike's measured figure). A single 237-instance request is the kind of
        thing that works on loopback and times out through a reverse proxy.
        """
        results: list[StowResult] = []
        for start in range(0, len(paths), batch_size):
            chunk = paths[start : start + batch_size]
            payloads = [(p.stem, p.read_bytes()) for p in chunk]
            results.append(self.stow(payloads, study_instance_uid=study_instance_uid))
        return results


def _sop_uid_from_part(part: MultipartPart, *, fallback: str) -> str:
    """Read `SOPInstanceUID` out of one retrieved instance.

    MOS-IMG-096 permits pydicom on the read path. The header is parsed from the bytes
    rather than trusted from a `Content-Location` header, because the header is the
    origin's claim and the dataset is the object; MOS-IMG-082's set comparison is only
    meaningful if the UIDs compared are the ones actually inside the instances.
    """
    import io

    import pydicom

    try:
        ds = pydicom.dcmread(io.BytesIO(part.content), stop_before_pixels=True, force=False)
    except Exception:  # noqa: BLE001 - a malformed part must not abort the whole retrieve
        return fallback
    return str(getattr(ds, "SOPInstanceUID", "") or fallback)
