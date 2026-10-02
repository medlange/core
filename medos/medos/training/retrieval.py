# SPDX-License-Identifier: Apache-2.0
"""The `dataset_export` retrieval boundary. `MOS-TRAIN-208` step 3, and the two digests.

`medos/api/v1/routes.yaml` recorded this as the third of row R26's three absences: "No
`dataset_export` retrieval adapter computes `MOS-EVID-018`'s `series_pixel_digest` or
`MOS-EVID-034`'s `dhash64`; `seal_from_batch` takes a `retrieve` callable and every
caller in this tree is a test. That third gap is what decides whether the check battery
reports `pass` or `skipped` for L3 and L4."

This module is that adapter, and the answer it returns on THIS deployment is `skipped`.
The rest of this docstring is why, because the difference between a skip that was
measured and a skip that was assumed is the whole value of the disclosure.

THE GATEWAY REFUSES `dataset_export`, BY DESIGN, AND THAT IS THE BINDING FACT
-----------------------------------------------------------------------------
`MOS-DATA-006` makes the Gateway the only route to DICOM. `MOS-TRAIN-068` and
`MOS-TRAIN-199` require the pipeline to acquire imaging ONLY from the de-identified side
of it, as the `dataset_export` consumer class. And `medos/medos/gateway/app.py` carries:

    _NO_DEID_CLASSES = frozenset({"clinical_viewer", "platform_writer"})
    if not write and consumer_class not in _NO_DEID_CLASSES:
        raise refuse(503, "DEID_NOT_IMPLEMENTED", ...)

`dataset_export` is not in that set. Every retrieval on this deployment under the only
consumer class the training pipeline is permitted to use is answered `503
DEID_NOT_IMPLEMENTED`, because the de-identification stage does not exist and
`MOS-DATA-037` requires the egress to fail closed rather than emit identified data.

So the pixels are not merely unfetched. They are unfetchable, by a control that is
working exactly as specified. The three things this module MUST NOT do about that:

  * IT MUST NOT RETRIEVE AS `platform_writer`, AND THAT IS ENFORCED BY THE CREDENTIAL IT
    IS NOT ALLOWED TO BORROW. The consumer class is resolved SERVER-SIDE from the
    principal (`medos/medos/gateway/auth.py::resolve_consumer_class`), so `CONSUMER_CLASS`
    below is what this module is entitled to and NOT what the Gateway would grant it:
    presenting the worker's key would be served as `platform_writer`, whose egress row
    reads "De-identification on egress: none" (`MOS-DATA-021`), and identified pixels
    would flow into a training corpus while every response looked correct. That is the
    worst combination available -- a seal that succeeds and is wrong -- and it was
    reachable in the first draft of this module, which read `MEDOS_DICOMWEB_URL` and
    `MEDOS_DICOMWEB_TOKEN`: the worker's pair, the `svc:medos-worker` key,
    `platform_writer`. So `gateway_from_env` below reads its OWN token variable,
    `MEDOS_DATASET_EXPORT_TOKEN`, refuses to fall back to the worker's, and reports a
    stated reason when it is unset -- which it is on this deployment, because
    `medos/deploy/compose/gateway-principals.json` declares no `dataset_export` principal at
    all. The URL may be shared; a URL is not a credential.
  * IT MUST NOT SYNTHESISE A PIXEL DIGEST AND CALL IT ONE. See the next section.
  * IT MUST NOT SILENTLY SKIP. `MOS-EVID-037` requires an explicit reason and
    `MOS-API-112` makes the wire form enforce it; `medos/medos/training/seal.py` renders what
    this module reports, and the `SealRun` schema refuses a `pass` while
    `series_without_pixel_evidence` is non-zero.

WHAT `series_pixel_digest` CARRIES WHEN THERE ARE NO PIXELS, AND WHY IT IS NOT A LIE
-------------------------------------------------------------------------------------
`SeriesRecord.series_pixel_digest` is REQUIRED (`medos/medos/evidence/manifest.py`) and
`dataset_cases.series_pixel_digest` is `sha256_digest NOT NULL` with a
`^sha256:[0-9a-f]{64}$` domain, so there is no sentinel and no null to write. The record
must carry a well-formed digest or the cohort cannot be sealed at all.

This module writes a digest over the SERIES IDENTITY, under an explicit namespace string
that is part of the hashed input:

    sha256("medicalos/series-identity/v1\\n" + study_uid + "\\n" + series_uid + "\\n"
           + "\\n".join(sop_instance_uids))

Three properties make that honest rather than a forgery:

  1. IT IS NOT RECOMPUTABLE AS `MOS-EVID-018`. A reader who re-derives the requirement's
     digest from the images gets a different value, so the manifest cannot be mistaken
     for one that was verified against pixels. A digest derived from the pixel VALUES by
     some other route -- a header hash, say -- would collide with the real thing's
     namespace and would be indistinguishable from it.
  2. IT MAKES L3 VACUOUS, AND L3 IS THEREFORE REPORTED `skipped` AND NEVER `pass`.
     Distinct series have distinct UIDs, so an identity-derived digest never collides
     across partitions and `medos/medos/evidence/leakage.py`'s L3 would report `pass` over a
     check that examined nothing. That is exactly the quiet downgrade the `SealRun`
     schema was written against, so `pixel_evidence` below reports the count and
     `medos/medos/training/seal.py` turns it into `outcome: "skipped"` with `skipped_reason`
     and `operator_disclosure`. The pass direction is the only one constrained: a real
     collision found among series that WERE hashed is a real finding.
  3. IT IS RECORDED. `RetrievalReport.pixel_evidence` is written onto the `seal_runs`
     row and rendered on rows R26 and R27, so "this cohort's digests are identity
     digests" is a durable, queryable property of the seal and not a comment.

  4. AND THE FROZEN SPLIT SAYS THE SAME THING. This was very nearly the one hole left in
     the design. `medos/medos/evidence/leakage.py::_l3` had no `skipped` path -- `_l4` has one,
     because `dhash64` is OPTIONAL on the record and its absence is self-describing,
     while `series_pixel_digest` is not -- so a cohort sealed from identity digests would
     have frozen a split whose stored `leakage_report` recorded `L3: pass`. `MOS-EVID-035`
     stores that report VERBATIM and `MOS-EVID-013` makes the row immutable, so the
     `SealRun` would have said `skipped` and the permanent record would have said the
     opposite, about the same cohort, for ever. `leakage_report` now takes
     `series_without_pixel_evidence`, `freeze_split` forwards it, and
     `medos/medos/training/seal.py` passes the series this module could not hash. Default-off:
     a caller that names none gets exactly the previous behaviour.
     REPORTED as an addition to chapter 7's freeze signature, for that chapter to adopt,
     rename or replace. `medos/schemas/evidence/dataset-split-1.0.0.json` already admitted
     `skipped` for all five checks and already said "`skipped` is not `pass`" -- the
     schema was ahead of the engine, and this closes the gap rather than opening one.

WHAT HAPPENS ON A DEPLOYMENT WHERE THE GATEWAY DOES SERVE `dataset_export`
---------------------------------------------------------------------------
`_retrieve_pixels` below is written and is not dead code behind a flag: it issues the
WADO-RS retrieval, decodes with `pydicom`, computes `MOS-EVID-018`'s digest over the
stored pixel arrays in canonical slice order, and computes `MOS-EVID-034` L4's `dhash64`
over the mid-axial slice. The moment `_NO_DEID_CLASSES` grows a de-identifying
`dataset_export` path, the same seal route produces `pass`/`fail` for L3 and L4 with no
further change here -- and until then every attempt is made, the refusal is recorded with
its HTTP status, and the report says which of the two it was. A module that skipped
WITHOUT trying could not tell "the Gateway refused" from "nobody configured a Gateway",
and those have different remedies.

NO PHI. `MOS-SEC-105` and `MOS-TRAIN-079`: nothing here logs or returns a patient name, a
birth date, an accession number or a source-space UID. The de-identified
`StudyInstanceUID` is permitted in a response by `MOS-API-006` and appears only in
refusal detail; `institution_key` is `MOS-TRAIN-089`'s HMAC, taken verbatim from the
candidate row, and this module never sees an institution name.

Spec: MOS-DATA-006, MOS-DATA-021, MOS-DATA-037, MOS-EVID-017, MOS-EVID-018, MOS-EVID-020,
MOS-EVID-034, MOS-EVID-037, MOS-IMG-036, MOS-SEC-105, MOS-TRAIN-068, MOS-TRAIN-079,
MOS-TRAIN-084, MOS-TRAIN-087, MOS-TRAIN-089, MOS-TRAIN-199, MOS-TRAIN-200,
MOS-TRAIN-208, MOS-API-112.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from medos.evidence.manifest import ACQUISITION_FIELDS, Acquisition, SeriesRecord

__all__ = [
    "CONSUMER_CLASS",
    "DATASET_EXPORT_TOKEN_VAR",
    "DATASET_EXPORT_URL_VAR",
    "DEFAULT_MODALITY",
    "DEFAULT_SOP_CLASS_UID",
    "IDENTITY_DIGEST_NAMESPACE",
    "DatasetExportRetriever",
    "PixelEvidence",
    "SeriesPixels",
    "SeriesRetrieval",
    "gateway_from_env",
    "identity_digest",
    "series_pixel_digest",
]

log = logging.getLogger("medos.training.retrieval")

#: `MOS-TRAIN-068` and `MOS-TRAIN-199`. The ONLY consumer class the pipeline may use, and
#: a constant rather than a parameter because a retriever that could be pointed at
#: `clinical_viewer` is a retriever somebody will point at it.
CONSUMER_CLASS: Final[str] = "dataset_export"

#: The namespace that keeps an identity digest from ever being mistaken for
#: `MOS-EVID-018`'s. It is hashed INTO the value, not prefixed to it, so the digest still
#: satisfies the `sha256_digest` domain while being unreachable from the requirement's
#: own algorithm.
IDENTITY_DIGEST_NAMESPACE: Final[bytes] = b"medicalos/series-identity/v1\n"

#: The two manifest-line members `harvest_candidates` has no column for. See the block
#: comment at the call site in `__call__` for the full argument; the short form is that
#: these are an assumption this deployment can defend and not an imputation of an
#: `acquisition.*` field, which `MOS-EVID-020` forbids outright.
DEFAULT_MODALITY: Final[str] = "CT"
DEFAULT_SOP_CLASS_UID: Final[str] = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage

#: Which retrieval path this deployment takes. `gateway` attempts the real thing;
#: `metadata_only` declares up front that it will not and is for a deployment with no
#: Gateway at all. There is deliberately NO value that retrieves as another consumer
#: class -- see the module docstring.
RETRIEVAL_MODE_VAR: Final[str] = "MEDOS_SEAL_RETRIEVAL"

#: The pipeline's OWN Gateway credential. Deliberately NOT `MEDOS_DICOMWEB_TOKEN`, which
#: is the worker's and resolves to `platform_writer`. See the module docstring.
DATASET_EXPORT_TOKEN_VAR: Final[str] = "MEDOS_DATASET_EXPORT_TOKEN"

#: The DICOMweb root for that credential. Falls back to the worker's URL because a URL
#: is not a credential and `MOS-DATA-006` makes the Gateway the only route either way.
DATASET_EXPORT_URL_VAR: Final[str] = "MEDOS_DATASET_EXPORT_DICOMWEB_URL"


def gateway_from_env(env: Mapping[str, str] | None = None) -> tuple[Any, str]:
    """A Gateway bound to a `dataset_export` credential, or `(None, reason)`.

    Returns a PAIR because "no Gateway" and "a Gateway this process may not use as
    `dataset_export`" are different facts with different remedies, and a retriever that
    could not tell them apart would report one of them wrongly in a `SealRun` an
    operator reads.

    It never falls back to `MEDOS_DICOMWEB_TOKEN`. That is the single line that keeps
    `MOS-TRAIN-068` true at runtime rather than in a comment.
    """
    source = dict(env if env is not None else os.environ)
    token = source.get(DATASET_EXPORT_TOKEN_VAR, "").strip()
    if not token:
        return None, (
            f"{DATASET_EXPORT_TOKEN_VAR} is unset, so this process holds no "
            "dataset_export credential. MOS-TRAIN-068 and MOS-TRAIN-199 permit the "
            "pipeline to acquire imaging under that consumer class and no other, and "
            "the worker's MEDOS_DICOMWEB_TOKEN is deliberately NOT used here: the "
            "Gateway resolves it as platform_writer, whose egress carries no "
            "de-identification (MOS-DATA-021)"
        )
    url = (
        source.get(DATASET_EXPORT_URL_VAR, "").strip()
        or source.get("MEDOS_DICOMWEB_URL", "").strip()
    )
    if not url:
        return None, (
            f"neither {DATASET_EXPORT_URL_VAR} nor MEDOS_DICOMWEB_URL names a DICOMweb "
            "root, and MOS-DATA-006 makes the Gateway the only route to DICOM"
        )
    from medos.dicomweb.gateway import DicomWebGateway, GatewayConfig, PacsCredentials

    config = GatewayConfig(
        base_url=url,
        credentials=PacsCredentials(bearer_token=token),
        timeout_s=float(source.get("MEDOS_DICOMWEB_TIMEOUT_S") or 300.0),
    )
    return DicomWebGateway(config), ""


def series_pixel_digest(stored_arrays: Sequence[Any]) -> str:
    """`MOS-EVID-018`, transcribed. Hashes STORED pixel values, no rescale, no geometry.

    Transcribed rather than improved, for the reason that requirement gives in its own
    text: "so that dataset identity does not change when the geometry code changes". A
    digest that applied `RescaleSlope` would make the cohort's identity a function of the
    header, and re-exporting the same images from a PACS that normalises slope would mint
    a new cohort out of the same pixels.

    `stored_arrays` MUST already be in chapter 4's canonical slice order; ordering is the
    caller's because ordering is geometry and this module does not do geometry.
    """
    import numpy as np

    h = hashlib.sha256()
    for arr in stored_arrays:
        h.update(hashlib.sha256(np.ascontiguousarray(arr).tobytes()).digest())
    return "sha256:" + h.hexdigest()


def identity_digest(
    *, study_instance_uid: str, series_instance_uid: str, sop_instance_uids: Sequence[str]
) -> str:
    """A well-formed digest that is NOT `MOS-EVID-018`'s and cannot be confused with one.

    See the module docstring for the three properties that make this honest. The short
    form: it is required by the column domain, it is unreachable from the requirement's
    algorithm, it makes L3 vacuous, and the run says so on the wire.
    """
    h = hashlib.sha256()
    h.update(IDENTITY_DIGEST_NAMESPACE)
    h.update(study_instance_uid.encode("utf-8"))
    h.update(b"\n")
    h.update(series_instance_uid.encode("utf-8"))
    for uid in sop_instance_uids:
        h.update(b"\n")
        h.update(uid.encode("utf-8"))
    return "sha256:" + h.hexdigest()


@dataclass(frozen=True)
class SeriesPixels:
    """What a successful `dataset_export` retrieval of ONE series yields."""

    series_pixel_digest: str
    dhash64: int | None
    instances_retrieved: int


@dataclass(frozen=True)
class SeriesRetrieval:
    """The outcome for one series: pixels, or the stated reason there are none."""

    series_instance_uid: str
    pixels: SeriesPixels | None
    reason: str = ""
    http_status: int | None = None

    @property
    def held_pixels(self) -> bool:
        return self.pixels is not None


@dataclass
class PixelEvidence:
    """`SealRun.pixel_evidence`, accumulated as the retrieval runs.

    A mutable accumulator and not a frozen value: it is written by the driver at each
    phase so that `GET /api/v1/seal-runs/{id}` can answer `MOS-UI-134`'s
    "progress-reported step with the cohort size shown" while the step is still running.
    """

    series_total: int = 0
    series_with_pixel_digest: int = 0
    series_with_perceptual_hash: int = 0
    instances_total: int = 0
    instances_retrieved: int = 0
    #: One entry per distinct refusal, with its count. Distinct rather than per-series so
    #: that a cohort of 150 series does not produce 150 identical sentences.
    reasons: dict[str, int] = field(default_factory=dict)
    http_statuses: dict[int, int] = field(default_factory=dict)

    def record(self, outcome: SeriesRetrieval) -> None:
        self.series_total += 1
        if outcome.pixels is not None:
            self.series_with_pixel_digest += 1
            self.instances_retrieved += outcome.pixels.instances_retrieved
            if outcome.pixels.dhash64 is not None:
                self.series_with_perceptual_hash += 1
        elif outcome.reason:
            self.reasons[outcome.reason] = self.reasons.get(outcome.reason, 0) + 1
            if outcome.http_status is not None:
                self.http_statuses[outcome.http_status] = (
                    self.http_statuses.get(outcome.http_status, 0) + 1
                )

    @property
    def series_without_pixel_digest(self) -> int:
        return self.series_total - self.series_with_pixel_digest

    @property
    def series_without_perceptual_hash(self) -> int:
        return self.series_total - self.series_with_perceptual_hash

    def as_dict(self) -> dict[str, Any]:
        """The `pixel_evidence` member of `SealRun`. `consumer_class` is a const."""
        return {
            "consumer_class": CONSUMER_CLASS,
            "series_total": self.series_total,
            "series_with_pixel_digest": self.series_with_pixel_digest,
            "series_with_perceptual_hash": self.series_with_perceptual_hash,
        }

    def stated_reason(self) -> str:
        """One sentence naming why pixels are absent, with counts. Never empty when they
        are: `MOS-EVID-037` requires an EXPLICIT reason and "not available" is not one."""
        if not self.reasons:
            return ""
        parts = [f"{reason} ({n} series)" for reason, n in sorted(self.reasons.items())]
        return "; ".join(parts)


class _Retriever(Protocol):
    """The two methods this module needs off `medos.dicomweb.gateway.DicomWebGateway`."""

    def instance_uids(
        self, study_instance_uid: str, series_instance_uid: str
    ) -> set[str]: ...

    def fetch_series(
        self, study_instance_uid: str, series_instance_uid: str, dest: Any, **kw: Any
    ) -> Any: ...


def _acquisition_from_profile(profile: Mapping[str, Any]) -> Acquisition:
    """`MOS-TRAIN-084`'s thirteen candidate fields as chapter 7's `Acquisition`.

    The two vocabularies overlap but are not the same: the candidate carries
    `exposure_mas`, `iterative_recon_strength` and `station_key`, which chapter 7's
    manifest line does not, and the manifest line carries `z_coverage_mm`, `image_type`,
    `patient_age_years`, `patient_sex` and `body_part_examined`, which the candidate
    profile does not. A missing value is `None` and NEVER a default (`MOS-EVID-020`): "a
    default slice thickness of 1.0 mm on a header that carried none" is the defect that
    requirement names, and it would land in an immutable manifest.
    """
    spacing = profile.get("pixel_spacing_mm")
    pixel_spacing: tuple[float, float] | None = None
    if isinstance(spacing, (list, tuple)) and len(spacing) == 2:
        pixel_spacing = (float(spacing[0]), float(spacing[1]))
    return Acquisition(
        slice_thickness_mm=_as_float(profile.get("slice_thickness_mm")),
        pixel_spacing_mm=pixel_spacing,
        convolution_kernel=_as_str(profile.get("convolution_kernel")),
        convolution_kernel_class=_as_str(profile.get("convolution_kernel_class")),
        manufacturer=_as_str(profile.get("manufacturer")),
        manufacturer_model_name=_as_str(profile.get("manufacturer_model_name")),
        kvp=_as_float(profile.get("kvp")),
        contrast_phase=_as_str(profile.get("contrast_phase")),
    )


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)


class DatasetExportRetriever:
    """The `retrieve` callable `medos.training.curation.seal_from_batch` takes.

    Constructed per seal run and never module-level: CONTRACT.md section 11 forbids
    global mutable state, and the accumulator below is state the run owns.

    `gateway` is the injected collaborator and may be `None`, which is the honest shape
    of a deployment with no Gateway configured -- distinguished in the report from a
    Gateway that refused, because the two have different remedies.
    """

    def __init__(
        self,
        *,
        gateway: _Retriever | None = None,
        work_dir: Any = None,
        mode: str | None = None,
        unavailable_reason: str = "",
    ) -> None:
        self.gateway = gateway
        self.unavailable_reason = unavailable_reason
        self.work_dir = work_dir
        self.mode = (mode or os.environ.get(RETRIEVAL_MODE_VAR, "gateway")).strip()
        self.evidence = PixelEvidence()
        self.outcomes: list[SeriesRetrieval] = []

    # -- the callable ----------------------------------------------------------------
    def __call__(self, candidate: Any) -> list[SeriesRecord]:
        """One `CandidateRow` -> its `SeriesRecord`s. `MOS-TRAIN-208` step 3.

        The metadata half comes from the candidate row and not from a second QIDO: the
        candidate already carries `MOS-TRAIN-084`'s profile, copied from the source
        header without imputation at draw time, and re-reading it here would be a second
        source for the same fact that can disagree with the first (`MOS-UI-116`'s
        argument, one layer down).
        """
        profile = dict(candidate.acquisition_profile or {})
        acquisition = _acquisition_from_profile(profile)
        institution_key = candidate.institution_key
        study_year = profile.get("study_year")
        series_uids = list(candidate.series_instance_uids)
        instance_uids = list(candidate.instance_uids)

        records: list[SeriesRecord] = []
        for series_uid in series_uids:
            # ONE SERIES, ALL THE CANDIDATE'S INSTANCES, when the candidate names one
            # series -- which is the only shape `add_candidate` produces today. A
            # multi-series candidate cannot be split across its `instance_uids` column
            # (it is a flat array with no series attribution), so a per-series instance
            # list is unavailable and the retrieval asks the Gateway for it. When the
            # Gateway is unreachable the flat list is used for the single-series case and
            # the multi-series case is refused rather than guessed at.
            if len(series_uids) == 1:
                sops = list(instance_uids)
            else:
                sops = self._instance_uids(candidate.study_instance_uid, series_uid)
                if not sops:
                    raise ValueError(
                        f"candidate {candidate.id} names {len(series_uids)} series and "
                        "harvest_candidates.instance_uids is a flat array with no series "
                        "attribution; per-series instances need a Gateway QIDO and this "
                        "deployment's Gateway did not answer. Refusing rather than "
                        "assigning every instance to every series (MOS-EVID-017)"
                    )

            outcome = self._retrieve_pixels(
                study_instance_uid=candidate.study_instance_uid,
                series_instance_uid=series_uid,
                expected_instances=len(sops),
            )
            self.outcomes.append(outcome)
            self.evidence.instances_total += len(sops)
            self.evidence.record(outcome)

            if outcome.pixels is not None:
                digest = outcome.pixels.series_pixel_digest
                dhash = outcome.pixels.dhash64
            else:
                digest = identity_digest(
                    study_instance_uid=candidate.study_instance_uid,
                    series_instance_uid=series_uid,
                    sop_instance_uids=sops,
                )
                # MOS-EVID-037: absent, so L4 reports `skipped` with an explicit reason
                # at freeze time rather than `pass`. `medos/medos/evidence/leakage.py::_l4`
                # already implements exactly that for `dhash64 is None`.
                dhash = None

            records.append(
                SeriesRecord(
                    patient_key=candidate.patient_key,
                    study_instance_uid=candidate.study_instance_uid,
                    series_instance_uid=series_uid,
                    # REPORTED GAP, and the one value in this record that is not read
                    # off something. `SeriesRecord.modality` and `sop_class_uid` are
                    # manifest-line members (`MOS-EVID-017`) and `harvest_candidates`
                    # has NO COLUMN for either: `add_candidate` receives the modality
                    # in its `study` argument for `MOS-TRAIN-075`'s scope test and does
                    # not persist it, and 0011_curation.up.sql section 5 has no place
                    # to put it. The Gateway's QIDO would answer both, and on this
                    # deployment the Gateway answers nothing.
                    #
                    # So the fallback below is an ASSUMPTION, named here rather than
                    # buried: this platform's only capability class is chest CT
                    # (`medos/medos/capabilities`), and the harvest scope of the walk's
                    # TrainingDataPolicy declares `modalities: ["CT"]`. It is not
                    # imputation of an `acquisition.*` member -- `MOS-EVID-020` governs
                    # those and every one of them comes off the candidate's profile or
                    # is `None`. Closing it is a column on `harvest_candidates`, which
                    # is chapter 17's and chapter 12's to add; until then a cohort of
                    # anything but CT would be sealed with a manifest line that says CT.
                    modality=str(profile.get("modality") or DEFAULT_MODALITY),
                    sop_class_uid=str(
                        profile.get("sop_class_uid") or DEFAULT_SOP_CLASS_UID
                    ),
                    sop_instance_uids=tuple(sops),
                    series_pixel_digest=digest,
                    acquisition=acquisition,
                    institution_key=institution_key,
                    study_year=int(study_year) if study_year is not None else None,
                    # MOS-EVID-034 L5 reads this and `MOS-TRAIN-079` forbids the
                    # accession number on a candidate in ANY form -- hashed or not. So
                    # L5 has nothing to compare and reports over an empty set. Named on
                    # the seal route rather than filled with a derived value: a hash of
                    # the study UID is not an accession hash, and L5 comparing it would
                    # be L2 under another name.
                    accession_number_hash=None,
                    corpus_generation=int(candidate.corpus_generation),
                    dhash64=dhash,
                )
            )
        return records

    # -- the real retrieval ----------------------------------------------------------
    def _instance_uids(self, study_uid: str, series_uid: str) -> list[str]:
        if self.gateway is None or self.mode == "metadata_only":
            return []
        try:
            return sorted(self.gateway.instance_uids(study_uid, series_uid))
        except Exception as exc:  # noqa: BLE001 - every failure is the same answer here
            log.info(
                "seal_retrieval_qido_failed",
                extra={"reason": type(exc).__name__, "series_ref": _ref(series_uid)},
            )
            return []

    def _retrieve_pixels(
        self, *, study_instance_uid: str, series_instance_uid: str, expected_instances: int
    ) -> SeriesRetrieval:
        """Attempt the `dataset_export` retrieval. Never raises; reports.

        A retrieval failure is not a seal failure -- `MOS-EVID-037` makes an absent
        perceptual hash a `skipped` check and not an error -- so every exception becomes
        a stated reason. What it MUST NOT become is silence.
        """
        if self.mode == "metadata_only":
            return SeriesRetrieval(
                series_instance_uid,
                None,
                reason=(
                    f"{RETRIEVAL_MODE_VAR}=metadata_only: this deployment declares that "
                    "it seals from candidate metadata and retrieves no pixels"
                ),
            )
        if self.gateway is None:
            return SeriesRetrieval(
                series_instance_uid,
                None,
                reason=self.unavailable_reason
                or (
                    "no DICOMweb Gateway is configured for this process, and "
                    "MOS-DATA-006 makes the Gateway the only route to DICOM"
                ),
            )
        if self.work_dir is None:
            return SeriesRetrieval(
                series_instance_uid,
                None,
                reason="no working directory was provided for the retrieval",
            )

        try:
            fetched = self.gateway.fetch_series(
                study_instance_uid,
                series_instance_uid,
                self.work_dir,
                verify_against_qido=True,
            )
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
            # MOS-API-042 and MOS-SEC-105: the exception TYPE and the HTTP status, never
            # the message -- a Gateway problem document quotes the path, and on
            # MOS-DATA-007's URL space the path carries a StudyInstanceUID.
            reason = (
                f"the Gateway refused or could not serve the {CONSUMER_CLASS} consumer "
                f"class ({type(exc).__name__}"
                + (f", HTTP {int(status)}" if isinstance(status, int) else "")
                + "). MOS-DATA-021 requires de-identification on egress for this class "
                "and MOS-DATA-037 requires the egress to fail closed rather than emit "
                "identified data"
            )
            log.info(
                "seal_retrieval_refused",
                extra={
                    "reason": type(exc).__name__,
                    "http_status": status if isinstance(status, int) else None,
                    "consumer_class": CONSUMER_CLASS,
                },
            )
            return SeriesRetrieval(
                series_instance_uid,
                None,
                reason=reason,
                http_status=status if isinstance(status, int) else None,
            )

        try:
            pixels = self._digest_and_hash(fetched)
        except Exception as exc:  # noqa: BLE001
            return SeriesRetrieval(
                series_instance_uid,
                None,
                reason=(
                    f"the series was retrieved and could not be decoded "
                    f"({type(exc).__name__}); MOS-EVID-018's digest is over stored pixel "
                    "values and a digest over bytes this process could not read would "
                    "not be that digest"
                ),
            )
        if expected_instances and pixels.instances_retrieved != expected_instances:
            # MOS-IMG-152's reasoning, one layer up: a short series hashed as a whole
            # series is a digest of the wrong thing, permanently, in a content-addressed
            # record.
            return SeriesRetrieval(
                series_instance_uid,
                None,
                reason=(
                    f"the retrieval returned {pixels.instances_retrieved} of "
                    f"{expected_instances} instances; MOS-EVID-017 requires the manifest "
                    "line to list every instance and a digest over a short series is a "
                    "digest of a different series"
                ),
            )
        return SeriesRetrieval(series_instance_uid, pixels)

    @staticmethod
    def _digest_and_hash(fetched: Any) -> SeriesPixels:
        """`MOS-EVID-018` over the retrieved files, plus `MOS-EVID-034` L4's `dhash64`.

        `dhash64` is computed only when `scipy` is importable, because
        `medos/medos/evidence/leakage.py::dhash64` uses `scipy.ndimage.zoom` and the constants
        of that implementation are the check -- a numpy-only resize would be a second
        variant and "a cohort hashed under one variant is not comparable with one hashed
        under another". When it is absent the digest is still computed and L4 is the only
        check that skips, which is a narrower and more honest outcome than skipping both.
        """
        import pydicom

        paths = sorted(getattr(fetched, "paths", ()))
        datasets = [pydicom.dcmread(str(p)) for p in paths]
        # Chapter 4's canonical slice order. ImagePositionPatient z is the axis when it
        # is present; InstanceNumber is the documented fallback and is recorded as such
        # rather than silently preferred.
        datasets.sort(key=_slice_sort_key)
        arrays = [ds.pixel_array for ds in datasets]
        digest = series_pixel_digest(arrays)

        dhash: int | None = None
        if arrays:
            try:
                from medos.evidence.leakage import dhash64 as _dhash64

                dhash = _dhash64(arrays[len(arrays) // 2])
            except Exception:  # noqa: BLE001 - scipy absent, or a non-axial series
                dhash = None
        return SeriesPixels(
            series_pixel_digest=digest, dhash64=dhash, instances_retrieved=len(arrays)
        )


def _slice_sort_key(ds: Any) -> tuple[float, int]:
    position = getattr(ds, "ImagePositionPatient", None)
    z = float(position[2]) if position is not None and len(position) == 3 else 0.0
    number = int(getattr(ds, "InstanceNumber", 0) or 0)
    return (z, number)


def _ref(uid: str) -> str:
    """`MOS-SEC-106`'s reference form: a short digest of a UID, never the UID.

    Used only in this module's own log lines. A `SeriesInstanceUID` is P2 on the Gateway
    surface (`MOS-SEC-105`) and a log line is exactly where one leaks.
    """
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:12]


# `ACQUISITION_FIELDS` is imported so that a change to chapter 7's line format shows up
# here as an import error rather than as a silently narrower manifest. It is asserted
# rather than used, because `_acquisition_from_profile` names its members explicitly --
# a loop over the tuple would put a field on the wire the moment the tuple grew, with
# nobody deciding that it should be there.
assert "slice_thickness_mm" in ACQUISITION_FIELDS
