# SPDX-License-Identifier: Apache-2.0
"""Deterministic identity: UID derivation, idempotency keys, de-identification.

CONTRACT.md §1: "derive_uid, derive_idempotency_key, deid_uid, JobIdentity". Lifted from
`spikes/week0/write_dicom_results.py` per CONTRACT.md §2.

------------------------------------------------------------------------------------
CONTRACT.md §2 defect #1: `derive_uid` was defined TWICE
------------------------------------------------------------------------------------
Copies lived in `spikes/week0/write_dicom_results.py` and
`spikes/week0/verify_roundtrip.py`. They were diffed before merging.

**They had NOT diverged behaviourally.** Byte-for-byte the same namespace UUID, the same
ten `name` components in the same order joined with `|`, the same `2.25.` + uuid5 form
and the same org-root + SHA-256/12-byte form. Every input maps to the same output under
both. The only differences were:

  1. Error message quality. write_dicom_results raised `ValueError(uid_kind)` -- the bad
     value with no statement of what was wrong. verify_roundtrip raised
     `ValueError(f"unknown uid_kind {uid_kind!r}")`.
  2. Docstring emphasis. write_dicom_results explained WHY the argument tuple is the
     identity (MOS-IMG-085). verify_roundtrip recorded the MOS-IMG-069 prohibition on
     ever minting a StudyInstanceUID.

**Kept: the diagnosable error messages, and the union of both docstrings.** The messages
because the losing variant tells an on-call engineer nothing; the union because both
facts are load-bearing and each copy had recorded only one of them -- which is precisely
how the duplicate survived review in the first place.

That the two copies agreed on output is the *only* reason this was a latent defect
rather than an active one. MOS-IMG-085 makes "same UID implies same declared inputs" an
invariant the retry path depends on, so the next edit to either copy would have
re-identified every object written by the other.

Spec: MOS-IMG-062, MOS-IMG-063, MOS-IMG-064, MOS-IMG-065, MOS-IMG-067, MOS-IMG-069,
MOS-IMG-071..078, MOS-IMG-085, MOS-EXEC-053, MOS-EXEC-054, MOS-DATA-031, MOS-DATA-032.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MEDICALOS_UID_NAMESPACE",
    "UID_KINDS",
    "SOP_CLASS_SEG",
    "SOP_CLASS_SR_COMPREHENSIVE_3D",
    "SERIES_NUMBER_OFFSETS",
    "derive_uid",
    "check_uid_length",
    "derive_idempotency_key",
    "deid_uid",
    "JobIdentity",
]


# MOS-IMG-062. Fixed for the life of the product; changing it re-identifies every object
# ever written, so it is a constant and never a configuration value.
MEDICALOS_UID_NAMESPACE = uuid.UUID("b4c1e0a6-8f2d-5d3a-9c17-0f6a2e8b41d5")


UID_KINDS = frozenset(
    {
        "seg.series",
        "seg.instance",
        "sr.series",
        "sr.instance",
        "sc.series",
        "sc.instance",
        "reg.series",
        "reg.instance",
        "frame_of_reference",
        "device_observer",
        "tracking",
    }
)


# MOS-IMG-059
SOP_CLASS_SEG = "1.2.840.10008.5.1.4.1.1.66.4"


SOP_CLASS_SR_COMPREHENSIVE_3D = "1.2.840.10008.5.1.4.1.1.88.34"


# MOS-IMG-073: offset and max series_index per kind, within a band of 10.
SERIES_NUMBER_OFFSETS = {"seg": (0, 3), "sr": (4, 2), "sc": (7, 2)}


def derive_uid(
    *,
    org_root: str | None,
    tenant_id: str,
    idempotency_key: str,
    service_id: str,
    service_version: str,
    model_id: str,
    model_version: str,
    uid_space: str,  # "source" | "deid"
    uid_kind: str,  # one of UID_KINDS
    output_index: int,  # >= 0, see MOS-IMG-065
) -> str:
    """MOS-IMG-062, verbatim.

    WHY it is transcribed rather than re-derived: the argument tuple *is* the identity.
    MOS-IMG-085 makes "same UID implies same declared inputs" an invariant the retry path
    depends on, so any re-ordering of the `name` components, any added or dropped field,
    silently re-identifies every object the deployment has ever written.

    Note what is ABSENT: there is no `StudyInstanceUID` parameter and no caller may
    pass one. MOS-IMG-069 forbids minting a study UID at all, and CI is required to
    assert that `derive_uid` never appears in the same expression as
    `StudyInstanceUID`. (Merged in from the second definition, which lived in
    spikes/week0/verify_roundtrip.py -- see the module docstring.)

    CONTRACT.md §9: a client-supplied `Idempotency-Key` header MUST NOT reach this
    function. The seed is `derive_idempotency_key`, derived server-side from job
    identity.
    """
    if uid_kind not in UID_KINDS:
        raise ValueError(f"unknown uid_kind {uid_kind!r}")
    if uid_space not in ("source", "deid"):
        raise ValueError(f"uid_space must be 'source' or 'deid', got {uid_space!r}")
    name = "|".join(
        (
            "MOS-IMG-UID-v1",
            tenant_id,
            idempotency_key,
            service_id,
            service_version,
            model_id,
            model_version,
            uid_space,
            uid_kind,
            str(output_index),
        )
    )
    if org_root is None:
        return "2.25." + str(uuid.uuid5(MEDICALOS_UID_NAMESPACE, name).int)
    digest12 = hashlib.sha256(name.encode("utf-8")).digest()[:12]
    return f"{org_root}.{int.from_bytes(digest12, 'big')}"


def check_uid_length(uid: str, org_root: str | None) -> None:
    """MOS-IMG-063: both forms must satisfy the UI VR limit of 64 by construction."""
    if len(uid) > 64:
        raise ValueError(f"derived UID exceeds the DICOM UI VR limit of 64: {uid!r}")
    if org_root is not None and len(org_root) > 34:
        raise ValueError(
            f"org_root {org_root!r} is {len(org_root)} chars; MOS-IMG-063 caps it at 34 "
            "so the derived UID cannot exceed 64"
        )
    for component in uid.split("."):
        if len(component) > 1 and component[0] == "0":
            raise ValueError(f"UID component with a leading zero: {uid!r}")


def derive_idempotency_key(
    *,
    tenant_id: str,
    service_id: str,
    service_version: str,
    study_instance_uid: str,
    selected_series_uids: Sequence[str],
    prior_study_instance_uids: Sequence[str] = (),
    requested_outputs: Sequence[str] = ("SEG", "SR"),
    parameters: dict[str, Any] | None = None,
) -> str:
    """MOS-EXEC-053, verbatim material set.

    MOS-EXEC-054: a client-supplied Idempotency-Key header MUST NOT reach this function -
    "Allowing a client to choose the key would let one tenant's caller collide two
    clinically distinct runs into one DICOM series identity." Hence keyword-only args
    naming exactly the permitted material and nothing that varies per attempt.
    """
    import base64

    material = {
        "v": 1,
        "tenant_id": tenant_id,
        "service_id": service_id,
        "service_version": service_version,
        "study_instance_uid": study_instance_uid,
        "series_instance_uids": sorted(selected_series_uids),
        "prior_study_instance_uids": sorted(prior_study_instance_uids),
        "requested_outputs": sorted(requested_outputs),
        "parameters": _canonicalise(parameters or {}),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(blob).digest()
    return "ik_" + base64.b32encode(digest).decode("ascii").rstrip("=")[:26].lower()


def _canonicalise(obj: Any) -> Any:
    """RFC 8785-ish canonicalisation sufficient for JSON-object parameters.

    Not a full JCS implementation: it normalises key order and rejects float NaN/Inf,
    which is the part that matters for key stability here. A production platform must
    use a real JCS library - flagged rather than silently approximated.
    """
    if isinstance(obj, dict):
        return {k: _canonicalise(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_canonicalise(v) for v in obj]
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        raise ValueError("non-finite float in job parameters: not canonicalisable")
    return obj


def deid_uid(tenant_deid_key: bytes, source_uid: str) -> str:
    """MOS-DATA-032, verbatim.

    Present here because MOS-IMG-067 forces `uid_space` into `derive_uid`, and because
    MOS-DATA-031 is a BLOCKING INVARIANT this spike must be able to demonstrate:
    the same source UID maps to the same surrogate forever, across processes.
    """
    digest = hmac.new(tenant_deid_key, source_uid.encode("ascii"), hashlib.sha256).digest()
    b = bytearray(digest[:16])
    b[6] = (b[6] & 0x0F) | 0x40  # UUID version 4
    b[8] = (b[8] & 0x3F) | 0x80  # RFC 4122 variant
    return "2.25." + str(uuid.UUID(bytes=bytes(b)).int)


@dataclass(frozen=True)
class JobIdentity:
    """Everything `derive_uid` and the equipment module need, resolved once.

    Frozen, because MOS-IMG-085's invariant is that identity is a pure function of this
    tuple: a field mutated between the SEG write and the SR write would produce two
    objects claiming a shared provenance they do not have.
    """

    tenant_id: str
    service_id: str
    service_version: str
    model_id: str
    model_version: str
    deployment_id: str
    institution_name: str
    legal_manufacturer_name: str
    medicalos_version: str
    preprocessing_spec_version: str
    preprocessing_spec_digest: str
    study_instance_uid: str
    selected_series_uids: tuple[str, ...]
    requested_outputs: tuple[str, ...]
    parameters: dict[str, Any]
    uid_space: str
    org_root: str | None
    series_number_band: int
    clinical_use_mode: str
    job_id: str

    @property
    def idempotency_key(self) -> str:
        return derive_idempotency_key(
            tenant_id=self.tenant_id,
            service_id=self.service_id,
            service_version=self.service_version,
            study_instance_uid=self.study_instance_uid,
            selected_series_uids=self.selected_series_uids,
            requested_outputs=self.requested_outputs,
            parameters=self.parameters,
        )

    def uid(self, uid_kind: str, output_index: int) -> str:
        u = derive_uid(
            org_root=self.org_root,
            tenant_id=self.tenant_id,
            idempotency_key=self.idempotency_key,
            service_id=self.service_id,
            service_version=self.service_version,
            model_id=self.model_id,
            model_version=self.model_version,
            uid_space=self.uid_space,
            uid_kind=uid_kind,
            output_index=output_index,
        )
        check_uid_length(u, self.org_root)
        return u

    def series_number(self, kind: str, series_index: int) -> int:
        """MOS-IMG-073: B + offset + series_index, with the band's per-kind cap."""
        offset, max_index = SERIES_NUMBER_OFFSETS[kind]
        if series_index > max_index:
            raise ValueError(
                f"series_band_exhausted: {kind} series_index={series_index} exceeds the "
                f"band allowance of {max_index} (MOS-IMG-073). Reject at ServiceVersion "
                "registration, never renumber at write time."
            )
        if not (9000 <= self.series_number_band <= 9890) or self.series_number_band % 10:
            raise ValueError(
                f"series_number_band={self.series_number_band} is not one of "
                "{9000, 9010, ..., 9890} (MOS-IMG-072); 9900-9999 is reserved for "
                "platform-generated objects."
            )
        return self.series_number_band + offset + series_index

    @property
    def device_serial_number(self) -> str:
        """MOS-IMG-075: DeviceSerialNumber = f"{service_id}@{service_version}"."""
        return f"{self.service_id}@{self.service_version}"

    @property
    def software_versions(self) -> list[str]:
        """MOS-IMG-075: [service_version, medicalos_version, prep:<spec version>]."""
        return [
            self.service_version,
            self.medicalos_version,
            f"prep:{self.preprocessing_spec_version}",
        ]

    def validate_equipment_identity(self) -> None:
        """MOS-IMG-076/077: fail BEFORE any object is built, never write a placeholder."""
        checks = {
            "Manufacturer": self.legal_manufacturer_name,
            "ManufacturerModelName": self.service_display_name,
            "DeviceSerialNumber": self.device_serial_number,
            "InstitutionName": self.institution_name,
            "StationName": self.deployment_id,
        }
        for attr, value in checks.items():
            if not value or not str(value).strip():
                raise ValueError(
                    f"dicom_equipment_identity_missing: {attr} is empty (MOS-IMG-076). "
                    "The platform MUST NOT write a DICOM object with a placeholder "
                    "manufacturer."
                )
            if len(str(value)) > 64:
                raise ValueError(
                    f"{attr} is {len(str(value))} chars; MOS-IMG-077 caps Type 1 "
                    "equipment values at 64 and forbids truncating at write time."
                )
            if "\\" in str(value):
                raise ValueError(
                    f"{attr} contains a backslash, the DICOM value delimiter "
                    "(MOS-IMG-077)."
                )
        for v in self.software_versions:
            if "\\" in v:
                raise ValueError("SoftwareVersions component contains a backslash")
        if self.deployment_id.count(".") >= 3 or self.deployment_id.replace(".", "").isdigit():
            raise ValueError(
                f"StationName={self.deployment_id!r} looks like a hostname or IP; "
                "MOS-IMG-078 forbids leaking infrastructure topology into patient records."
            )

    @property
    def service_display_name(self) -> str:
        """Stand-in for `ServiceVersion.display_name` (MOS-IMG-075 value source)."""
        return f"MedicalOS {self.service_id} {self.service_version}"
