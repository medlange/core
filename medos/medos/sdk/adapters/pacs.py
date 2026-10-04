# SPDX-License-Identifier: Apache-2.0
"""The PACS adapter: how the SDK fetches studies from and stores results to an archive.

`PacsAdapter` is the Protocol every pipeline run goes through: list a study's series,
fetch one series to files, store result objects back. `DicomWebPacs` is the shipped
driver over DICOMweb (QIDO/WADO/STOW), delegating to the platform's own
`medos.dicomweb.client.DicomWebClient` -- the SDK does not reimplement DICOMweb, it
reuses the one client this repository maintains, and the lazy import keeps the base
install free of `requests`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from medos.sdk.adapters import DriverMissing

__all__ = [
    "SeriesRef",
    "PacsAdapter",
    "DicomWebPacs",
    "DriverMissing",
    "PacsDriverMissing",
]


#: Back-compat alias from the first revision; the class lives at the package root now.
PacsDriverMissing = DriverMissing


@dataclass(frozen=True)
class SeriesRef:
    """One series of a study, as the adapter lists it."""

    study_uid: str
    series_uid: str
    modality: str
    description: str = ""


#: QIDO-RS returns the DICOM JSON model (PS3.18): rows keyed by hex tags, each
#: `{"Value": [...], "vr": ...}`. Reading `row["SeriesInstanceUID"]` silently yields
#: nothing -- MEASURED against the shipped gateway on 2026-10-02, which is how the SDK
#:`s first live run caught it.
def _tag(row: Mapping[str, Any], tag: str, default: str = "") -> str:
    values = (row.get(tag) or {}).get("Value") or []
    return str(values[0]) if values else default


_TAG_SERIES_UID = "0020000E"
_TAG_MODALITY = "00080060"
_TAG_SERIES_DESCRIPTION = "0008103E"


class PacsAdapter(Protocol):
    """The archive side of a pipeline run. Implementations are stateless-ish handles."""

    def list_series(self, study_uid: str) -> list[SeriesRef]:
        """Every series of the study the caller may read."""
        ...

    def fetch_series(self, study_uid: str, series_uid: str, into: Path) -> Sequence[Path]:
        """Write the series' instances under `into` and return the files, in slice order."""
        ...

    def store(self, files: Sequence[Path], study_uid: str) -> None:
        """Store result objects (SEG/SR) into the study. Raises on partial store."""
        ...


@dataclass
class DicomWebPacs:
    """`PacsAdapter` over DICOMweb, via the platform's `DicomWebClient`.

    `base_url` names the DICOMweb tenant prefix (the Gateway's, in the shipped stack);
    `token` is the bearer the deployment issues. Construction imports the client --
    which imports `requests` -- so a process that never talks to a PACS never pays for
    it.
    """

    base_url: str
    token: str = ""
    timeout_s: float = 60.0

    def __post_init__(self) -> None:
        try:
            from medos.dicomweb.client import DicomWebClient  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - exercised by integration
            raise DriverMissing(
                "DicomWebPacs needs the DICOMweb client, which imports `requests`. "
                "Install the SDK with its server dependencies (`pip install medos[server]`) "
                "or pass an adapter whose transport you already carry."
            ) from exc
        self._client: Any = DicomWebClient(
            self.base_url, bearer_token=self.token or None, timeout_s=self.timeout_s
        )

    def list_series(self, study_uid: str) -> list[SeriesRef]:
        rows = self._client.qido_series(study_uid)
        return [
            SeriesRef(
                study_uid=study_uid,
                series_uid=_tag(row, _TAG_SERIES_UID),
                modality=_tag(row, _TAG_MODALITY),
                description=_tag(row, _TAG_SERIES_DESCRIPTION),
            )
            for row in rows
        ]

    def fetch_series(self, study_uid: str, series_uid: str, into: Path) -> Sequence[Path]:
        into.mkdir(parents=True, exist_ok=True)
        return self._client.retrieve_series_to_dir(study_uid, series_uid, into)

    def store(self, files: Sequence[Path], study_uid: str) -> None:
        results = self._client.stow_files(list(files), study_instance_uid=study_uid)
        failed = [r for r in results if getattr(r, "failed", None)]
        if failed:
            raise RuntimeError(
                f"STOW stored {len(results) - len(failed)} of {len(results)} batches; "
                f"{len(failed)} batches reported failed SOPs"
            )
