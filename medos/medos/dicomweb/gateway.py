# SPDX-License-Identifier: Apache-2.0
"""`DicomWebGateway` -- the single choke point for PACS access.

CONTRACT.md §1: "gateway.py  # the ONLY holder of PACS credentials in later releases".
In THIS slice that is nearly all it is: it holds the base URL and the credentials, builds
the one `DicomWebClient`, and exposes PACS operations in the vocabulary the worker speaks
(fetch this series, is this derived series already stored, store these objects). It is
deliberately thin. It is not deliberately pointless.

WHY THE INDIRECTION EXISTS NOW RATHER THAN AT WEEKS 3-5
    Every obligation chapter 3 puts on the Gateway is a property of a *single* place that
    all PACS traffic passes through:

      * MOS-DATA-009  the effective tenant comes from the principal, never the URL
      * MOS-DATA-011  QIDO results are filtered AFTER the backend answers
      * MOS-DATA-021  egress behaviour (de-identification, UID space) is chosen by
                      consumer class
      * MOS-DATA-024  quarantined instances are invisible on every route
      * MOS-DATA-026  one metric family covers every operation

    None of those are implementable in this slice -- there is no tenancy, no principal, no
    de-identification and no quarantine (CONTRACT.md §0). All of them become *edits to
    this file* rather than a search for every `requests.get` in the tree, which is the
    only reason to write the seam before the thing that needs it. A `DicomWebClient`
    constructed anywhere else is the defect this module exists to make visible.

CREDENTIALS
    `PacsCredentials` never renders its secret: `__repr__` is overridden, and the dataclass
    is excluded from any `asdict`-style dump by having the gateway expose
    `config_summary()` instead. Nothing in `medos` outside this module reads
    `MEDOS_DICOMWEB_PASSWORD` / `MEDOS_DICOMWEB_TOKEN`.

PHI
    CONTRACT.md §11: UIDs only, never a name or an MRN. `describe_series()` returns
    Modality, instance count and UIDs -- it deliberately does NOT return PatientName,
    PatientID, PatientBirthDate or StudyDescription, even though QIDO-RS hands them to us,
    because a selection log that carries them is a PHI leak in the audit trail.

Spec: MOS-DATA-007, MOS-DATA-009, MOS-DATA-011, MOS-DATA-021, MOS-DATA-023, MOS-DATA-024,
MOS-DATA-026, MOS-DATA-058, MOS-IMG-081, MOS-IMG-082, MOS-IMG-084, MOS-IMG-085,
MOS-IMG-152, MOS-OPS-005.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from medos.core.errors import TransportFailure
from medos.dicomweb.client import (
    TAG_MODALITY,
    TAG_NUMBER_OF_SERIES_RELATED_INSTANCES,
    TAG_SERIES_INSTANCE_UID,
    DicomWebClient,
    StowResult,
    _first,
)

__all__ = [
    "PacsCredentials",
    "GatewayConfig",
    "DicomWebGateway",
    "FetchedSeries",
    "SeriesSummary",
    "DEFAULT_DICOMWEB_URL",
]

# A last-resort default, and NOT what any deployment in this repository uses. Orthanc
# publishes no host port -- it is alone with the Gateway on an `internal: true` network
# (MOS-DATA-006) -- so this address answers nothing, which is the point: a default that
# cannot reach a real PACS by accident, and that fails closed and loudly with a refused
# connection rather than reaching something unintended. Every real caller sets
# `MEDOS_DICOMWEB_URL`; compose sets it to the Gateway's tenant-scoped root. An earlier
# version of this comment called this "the loopback DICOMweb root published by
# docker-compose.yml", which stopped being true when that ports block was deleted
# (register entry 70).
DEFAULT_DICOMWEB_URL = "http://127.0.0.1:8042/dicom-web"

ENV_URL = "MEDOS_DICOMWEB_URL"
ENV_USER = "MEDOS_DICOMWEB_USER"
ENV_PASSWORD = "MEDOS_DICOMWEB_PASSWORD"  # a variable NAME, not a secret
ENV_TOKEN = "MEDOS_DICOMWEB_TOKEN"  # a variable NAME, not a secret
ENV_TIMEOUT = "MEDOS_DICOMWEB_TIMEOUT_S"
ENV_VERIFY_TLS = "MEDOS_DICOMWEB_VERIFY_TLS"


@dataclass(frozen=True)
class PacsCredentials:
    """What the gateway is allowed to know about how to authenticate to the PACS.

    Frozen, and it never prints itself. A credential that reaches a log line, a traceback
    or a JSON report has escaped this module, and the point of CONTRACT.md §1's "the ONLY
    holder of PACS credentials" is that there is exactly one place to audit for that.
    """

    user: str | None = None
    password: str | None = None
    bearer_token: str | None = None

    @property
    def present(self) -> bool:
        return bool(self.user or self.bearer_token)

    @property
    def scheme(self) -> str:
        """What kind of credential this is, safe to log. Never the value."""
        if self.bearer_token:
            return "bearer"
        if self.user:
            return "basic"
        return "none"

    def __repr__(self) -> str:
        return f"PacsCredentials(scheme={self.scheme!r}, value=<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class GatewayConfig:
    """Where the PACS is and how to talk to it.

    `base_url` is the DICOMweb ROOT. Chapter 3's public routes carry a `{t}` tenant
    segment (`/dicomweb/{t}/studies`, MOS-DATA-007); this slice has no tenancy
    (CONTRACT.md §0) so there is no segment to fill, and weeks 3-5 adds it here.
    """

    base_url: str = DEFAULT_DICOMWEB_URL
    credentials: PacsCredentials = PacsCredentials()
    timeout_s: float = 300.0
    verify_tls: bool = True
    stow_batch_size: int = 20

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> GatewayConfig:
        """Read the configuration from the environment.

        This is the ONLY place in `medos` that reads a PACS credential variable. A second
        reader is how "the gateway is the only credential holder" silently stops being
        true, so the names are module constants and this classmethod is the one consumer.
        """
        src = os.environ if env is None else env
        verify = str(src.get(ENV_VERIFY_TLS, "true")).strip().lower()
        return cls(
            base_url=src.get(ENV_URL) or DEFAULT_DICOMWEB_URL,
            credentials=PacsCredentials(
                user=src.get(ENV_USER) or None,
                password=src.get(ENV_PASSWORD) or None,
                bearer_token=src.get(ENV_TOKEN) or None,
            ),
            timeout_s=float(src.get(ENV_TIMEOUT) or 300.0),
            verify_tls=verify not in ("0", "false", "no", "off"),
        )


@dataclass(frozen=True)
class SeriesSummary:
    """The PHI-free description of one candidate series.

    CONTRACT.md §11 forbids logging a PHI value, and a series-selection decision IS
    logged -- it becomes `job_series` rows and an OHIF panel. So this record carries UIDs,
    a modality and a count, and deliberately drops PatientName, PatientID and
    StudyDescription even though QIDO-RS returns them in the same response.
    """

    study_instance_uid: str
    series_instance_uid: str
    modality: str
    n_instances: int


@dataclass(frozen=True)
class FetchedSeries:
    """One series pulled to local disk, ready for `build_canonical_volume`."""

    study_instance_uid: str
    series_instance_uid: str
    directory: Path
    paths: tuple[Path, ...]
    sop_instance_uids: tuple[str, ...]
    bytes_written: int

    @property
    def n_instances(self) -> int:
        return len(self.paths)


class DicomWebGateway:
    """The only object in `medos` that is permitted to reach a PACS.

    Constructed once per process (by the worker, or by a test fixture) and passed
    explicitly -- CONTRACT.md §11: "No global mutable state. Pass the connection; do not
    import a singleton."
    """

    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig()
        self._client = DicomWebClient(
            self.config.base_url,
            user=self.config.credentials.user,
            password=self.config.credentials.password,
            bearer_token=self.config.credentials.bearer_token,
            timeout_s=self.config.timeout_s,
            verify_tls=self.config.verify_tls,
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> DicomWebGateway:
        return cls(GatewayConfig.from_env(env))

    def __repr__(self) -> str:
        return f"DicomWebGateway(base_url={self.config.base_url!r})"

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DicomWebGateway:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def config_summary(self) -> dict[str, Any]:
        """A loggable description of this gateway. Never the credential value."""
        return {
            "base_url": self.config.base_url,
            "auth_scheme": self.config.credentials.scheme,
            "timeout_s": self.config.timeout_s,
            "verify_tls": self.config.verify_tls,
            "tenancy": "absent_in_this_slice",  # CONTRACT.md §0
        }

    # -- availability ----------------------------------------------------------------
    def preflight(self) -> None:
        """Raise `TransportFailure` unless the DICOMweb root answers."""
        self._client.preflight()

    def available(self) -> bool:
        """Non-raising preflight, for a readiness probe or a test skip condition."""
        try:
            self._client.preflight()
        except TransportFailure:
            return False
        return True

    # -- discovery (metadata only) ----------------------------------------------------
    def list_series(self, study_instance_uid: str) -> list[SeriesSummary]:
        """Enumerate a study's series using QIDO-RS only.

        MOS-DATA-058: selection reads metadata only and MUST NOT issue an instance, frame
        or rendered retrieve. This method is the whole of what a selection step is allowed
        to see, which is why it returns `SeriesSummary` and not raw QIDO rows -- a raw row
        carries PatientName, and a caller handed one will eventually log it.
        """
        rows = self._client.qido_series(study_instance_uid)
        out: list[SeriesSummary] = []
        for row in rows:
            raw_count = (row.get(TAG_NUMBER_OF_SERIES_RELATED_INSTANCES) or {}).get(
                "Value"
            ) or [0]
            out.append(
                SeriesSummary(
                    study_instance_uid=study_instance_uid,
                    series_instance_uid=_first(row, TAG_SERIES_INSTANCE_UID),
                    modality=_first(row, TAG_MODALITY),
                    n_instances=int(raw_count[0] or 0),
                )
            )
        return out

    def series_metadata(
        self, study_instance_uid: str, series_instance_uid: str
    ) -> list[dict[str, Any]]:
        """Per-instance metadata for one series, no pixel data (MOS-DATA-058)."""
        return self._client.wado_series_metadata(study_instance_uid, series_instance_uid)

    def instance_uids(self, study_instance_uid: str, series_instance_uid: str) -> set[str]:
        """The instance UID SET the archive holds for one series.

        The read behind MOS-IMG-081 reconciliation and MOS-IMG-082 completeness. A set,
        because MOS-IMG-082 says "comparing the **set** of SOPInstanceUIDs" and a count
        comparison passes while an instance is missing and an unrelated one is present.
        """
        return self._client.instance_uids(study_instance_uid, series_instance_uid)

    def series_exists(self, study_instance_uid: str, series_instance_uid: str) -> bool:
        """Whether the archive holds any instance of this series.

        MOS-IMG-080/081: on a retry of a job that already has a `dicom_output_manifest`,
        the platform MUST reconcile against the archive instead of re-running inference.
        """
        try:
            return bool(self.instance_uids(study_instance_uid, series_instance_uid))
        except TransportFailure as exc:
            if exc.reason_code == "dicomweb_root_not_found":
                return False  # 404 on a derived series means "not stored yet"
            raise

    # -- retrieval -------------------------------------------------------------------
    def fetch_series(
        self,
        study_instance_uid: str,
        series_instance_uid: str,
        dest_dir: Path,
        *,
        verify_against_qido: bool = True,
    ) -> FetchedSeries:
        """Pull one series to `dest_dir` and verify the instance SET that arrived.

        This is step `fetch_series` of the chapter 5 step plan and the first hop of the
        weeks 1-2 pipeline (`docs/spec/15-delivery.md` §15.2.3, "DICOMweb pull").

        `verify_against_qido` issues the QIDO-RS instance query first and compares the
        retrieved set against it (MOS-IMG-152: sets, not counts). It costs one extra
        metadata request and it is the difference between "the volume builder was handed
        127 of 130 slices" surfacing here as a `TransportFailure` and surfacing three
        steps later as a silently short volume with a plausible affine.

        Streaming: `DicomWebClient.iter_series_instances` decodes incrementally, so a
        400-instance series never sits in memory at once (MOS-DATA-023).
        """
        expected: list[str] | None = None
        if verify_against_qido:
            expected = sorted(
                self._client.instance_uids(study_instance_uid, series_instance_uid)
            )
            if not expected:
                raise TransportFailure(
                    "dicomweb_series_incomplete",
                    {
                        "study_instance_uid": study_instance_uid,
                        "series_instance_uid": series_instance_uid,
                    },
                    "QIDO-RS reports zero instances for this series; there is nothing "
                    "to retrieve",
                )
        paths = self._client.retrieve_series_to_dir(
            study_instance_uid,
            series_instance_uid,
            dest_dir,
            expected_sop_instance_uids=expected,
        )
        return FetchedSeries(
            study_instance_uid=study_instance_uid,
            series_instance_uid=series_instance_uid,
            directory=dest_dir,
            paths=tuple(paths),
            sop_instance_uids=tuple(p.stem for p in paths),
            bytes_written=sum(p.stat().st_size for p in paths),
        )

    def fetch_instance(
        self, study_instance_uid: str, series_instance_uid: str, sop_instance_uid: str
    ) -> bytes:
        """Retrieve a single instance as Part-10 bytes."""
        return self._client.wado_instance(
            study_instance_uid, series_instance_uid, sop_instance_uid
        )

    # -- storage ---------------------------------------------------------------------
    def store_files(
        self, paths: Sequence[Path], *, study_instance_uid: str | None = None
    ) -> list[StowResult]:
        """STOW-RS a batch of generated objects into an existing study.

        Returns every batch result rather than a single boolean: MOS-IMG-084 makes a
        partial store a reportable outcome, and `dicom_output_manifest` needs to record
        which instance the archive acknowledged.

        This method does NOT raise on a partial store. Use `assert_stored` (or read
        `StowResult.ok` yourself) at the point where the job's terminal state is decided
        -- the decision "is this job FAILED" belongs to the worker, not to the transport.
        """
        return self._client.stow_files(
            list(paths),
            study_instance_uid=study_instance_uid,
            batch_size=self.config.stow_batch_size,
        )

    def store_bytes(
        self,
        payloads: Sequence[tuple[str, bytes]],
        *,
        study_instance_uid: str | None = None,
    ) -> StowResult:
        """STOW-RS objects already in memory, as `(sop_instance_uid, part10_bytes)`."""
        return self._client.stow(payloads, study_instance_uid=study_instance_uid)

    def assert_stored(
        self,
        results: Sequence[StowResult],
        study_instance_uid: str,
        series_instance_uid: str,
        expected_sop_instance_uids: Sequence[str],
    ) -> set[str]:
        """The MOS-IMG-084 + MOS-IMG-152 acceptance check, in one call.

        1. Every batch must be `ok` (200 AND empty `FailedSOPSequence`).
        2. The QIDO-RS re-read of the derived series must contain the expected UID **set**.

        MOS-IMG-083 makes an *unexpected* instance in a derived series a job failure, so
        the surplus is reported too -- a derived series that holds an object nobody in this
        job wrote means a UID collision or a concurrent writer, and continuing would
        attach someone else's pixels to this job's provenance.

        Returns the set actually present, so a caller can record it.
        """
        bad = [r for r in results if not r.ok]
        if bad:
            raise TransportFailure(
                "dicomweb_stow_incomplete",
                {
                    "study_instance_uid": study_instance_uid,
                    "series_instance_uid": series_instance_uid,
                    "n_batches": len(results),
                    "n_bad_batches": len(bad),
                    "http_statuses": [r.http_status for r in bad],
                    "failed": [
                        {
                            "sop_instance_uid": f.sop_instance_uid,
                            "failure_reason": f.failure_reason,
                        }
                        for r in bad
                        for f in r.failed
                    ][:20],
                },
                "STOW-RS did not fully succeed; MOS-IMG-084 requires HTTP 200 with an "
                "empty FailedSOPSequence",
            )
        present = self.instance_uids(study_instance_uid, series_instance_uid)
        expected = set(expected_sop_instance_uids)
        missing = expected - present
        unexpected = present - expected
        if missing or unexpected:
            raise TransportFailure(
                "dicomweb_series_incomplete",
                {
                    "study_instance_uid": study_instance_uid,
                    "series_instance_uid": series_instance_uid,
                    "missing_sop_instance_uids": sorted(missing)[:20],
                    "unexpected_sop_instance_uids": sorted(unexpected)[:20],
                },
                "the derived series in the archive does not match the written set "
                "(MOS-IMG-152 set equality; MOS-IMG-083 forbids unexpected instances)",
            )
        return present
