# SPDX-License-Identifier: Apache-2.0
"""The provenance record: `MOS-SAFE-083` sections A-F, as a value.

`docs/spec/15-delivery.md` section 15.2.4 asks this block for "the full provenance
record". Weeks 1-2 wrote a partial one -- CONTRACT.md section 10's field set, folded into
`results` -- and `MOS-SAFE-083` says exactly what "full" adds and why:

    The previous specification's list is a subset of section A/D below; the widened
    fields -- which series were actually consumed, the preprocessing version, the
    de-identification policy version, the evidence dataset version, and where every
    output object landed -- are what turn a list of version strings into a reproducible
    record.

The test of this module is `MOS-SAFE-092`'s: a record must be sufficient, ON ITS OWN, to
reconstruct the run. Concretely, `tests/integration/test_audit_provenance.py` reads one
record and identifies the exact inputs and the exact outputs of the job WITHOUT querying
another table. Everything here exists to make that true, and `validate()` exists to make
it fail loudly at write time when it is not.

PURE. No database, no HTTP, no clock. `medos/medos/db/provenance.py` persists what this builds;
`medos/medos/worker/result_rows.py` fills it in from a `PipelineState`.

Spec: MOS-SAFE-003, MOS-SAFE-012, MOS-SAFE-016, MOS-SAFE-046, MOS-SAFE-082, MOS-SAFE-083,
MOS-SAFE-084, MOS-SAFE-085, MOS-SAFE-086, MOS-SAFE-090, MOS-SAFE-092, MOS-STORE-279.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = [
    "RECORD_SCHEMA_VERSION",
    "PHI_FORBIDDEN_KEYS",
    "SeriesVerdict",
    "GeneratedObject",
    "ModelRef",
    "PreprocessingSpecRef",
    "build_record",
    "validate",
    "ProvenanceIncomplete",
    "assert_no_phi",
    "consumed_inputs",
    "produced_outputs",
]

RECORD_SCHEMA_VERSION = "provenance/1.0.0"


class ProvenanceIncomplete(ValueError):
    """A record that cannot answer "what went in and what came out". Carries every
    violation, not the first, because a writer fixing one at a time is a writer making
    six round trips through a failing job."""

    def __init__(self, violations: list[str]) -> None:
        super().__init__("; ".join(violations))
        self.violations = violations


# MOS-SAFE-086: "The provenance record MUST NOT contain PHI beyond the tenant-scoped
# identifiers listed: no `PatientName`, no `PatientBirthDate`, no free-text clinical
# history, no `StudyDescription` copied from source, no accession number. This record is
# exported and must be safe to hand a reviewer."
#
# Key names, checked recursively, in both DICOM keyword and snake_case spellings, because
# the record is assembled from two vocabularies -- pydicom attributes on one side and
# database columns on the other -- and a scanner that knows only one of them passes the
# document that used the other.
PHI_FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        "patientname", "patient_name",
        "patientid", "patient_id",
        "patientbirthdate", "patient_birth_date", "birth_date", "dob",
        "patientage", "patient_age",
        "patientsex", "patient_sex",
        "patientaddress", "patient_address",
        "accessionnumber", "accession_number", "accession",
        "studydescription", "study_description",
        "seriesdescription_source", "referringphysicianname",
        "referring_physician_name", "institutionname", "institution_name",
        "institutionaddress", "operatorsname", "performingphysicianname",
        "otherpatientids", "other_patient_ids", "patientcomments",
        "clinical_history", "clinicalhistory", "mrn", "medical_record_number",
    }
)


@dataclass(frozen=True)
class SeriesVerdict:
    """One entry of `input.series_considered[]`. `MOS-SAFE-084`.

    "`input.series_considered[]` MUST include rejected series with their reasons. The
    most common silent clinical failure in radiology AI is analysing the wrong
    reconstruction; the record must show what else was on the table."

    `rejection_reason` is chapter 3's closed `SeriesRequirement` vocabulary
    (`MOS-DATA-069`) -- `image_type_excluded`, `slice_thickness_out_of_range` -- and NOT
    the job-level list of `jobs.reject_reason_code`. MOS-STORE-270 is explicit that the
    two lists are different and neither may borrow from the other.
    """

    series_instance_uid: str
    selected: bool
    series_number: int | None = None
    rejection_reason: str | None = None
    instance_count: int | None = None
    modality: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "series_instance_uid": self.series_instance_uid,
            "series_number": self.series_number,
            "selected": self.selected,
            "rejection_reason": self.rejection_reason,
            "instance_count": self.instance_count,
            "modality": self.modality,
        }


@dataclass(frozen=True)
class GeneratedObject:
    """One entry of `outputs[]`. `MOS-SAFE-083` section F.

    `destination.qido_verified_at` is nullable and `MOS-SAFE-085` says why it must be:
    "MUST be set only after a QIDO-RS query against the minted `SeriesInstanceUID`
    returned the expected instance count. A STOW-RS 200 is not proof of landing." A
    writer that fills it from the STOW response has recorded a claim, not a verification.
    """

    output_index: int
    kind: str  # SEG | SR | SC | MEASUREMENT | LABELMAP
    sop_class_uid: str
    series_instance_uid: str
    sop_instance_uids: tuple[str, ...]
    series_number: int | None = None
    destination_id: str = "primary"
    stow_endpoint: str | None = None
    accepts_research: bool = True
    stow_http_status: int | None = None
    qido_verified_at: datetime | None = None
    instance_count_verified: int | None = None
    object_store_uri: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None
    ai_derived: bool = True
    ruo: bool = True
    series_description: str | None = None
    verification_flag: str = "UNVERIFIED"
    supersedes: str | None = None
    superseded_by: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "output_index": self.output_index,
            "kind": self.kind,
            "sop_class_uid": self.sop_class_uid,
            "series_instance_uid": self.series_instance_uid,
            "series_number": self.series_number,
            "sop_instance_uids": list(self.sop_instance_uids),
            "destination": {
                "destination_id": self.destination_id,
                "stow_endpoint": self.stow_endpoint,
                "accepts_research": self.accepts_research,
                "stow_http_status": self.stow_http_status,
                "qido_verified_at": _stamp(self.qido_verified_at),
                "instance_count_verified": self.instance_count_verified,
            },
            "object_store_uri": self.object_store_uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            # MOS-SAFE-012 / MOS-IMG-119: the marking a viewer renders next to the finding.
            "marking": {
                "ai_derived": self.ai_derived,
                "ruo": self.ruo,
                "series_description": self.series_description,
                "verification_flag": self.verification_flag,
            },
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
        }


@dataclass(frozen=True)
class ModelRef:
    """One entry of `execution.models[]`, "copied verbatim from
    `ResultBundle.diagnostics.model_versions[]`" (`MOS-SVC-098`)."""

    role: str
    model_id: str
    version: str
    artifact_digest: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model_id": self.model_id,
            "version": self.version,
            "artifact_digest": self.artifact_digest,
        }


@dataclass(frozen=True)
class PreprocessingSpecRef:
    """One entry of `execution.preprocessing_specs[]` -- "the field whose absence made the
    old record non-reproducible" (`MOS-SAFE-083` section D)."""

    id: str
    version: str
    digest: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "version": self.version, "digest": self.digest}


def _stamp(value: datetime | None) -> str | None:
    """RFC 3339 with a `Z`, or None. The record is hashed (`MOS-SAFE-090`), so a
    timestamp must have exactly one serialisation -- `datetime.isoformat()` renders the
    same instant as `+00:00` or `Z` depending on tzinfo, and two spellings of one moment
    hash differently."""
    if value is None:
        return None
    text = value.astimezone(tz=value.tzinfo).isoformat() if value.tzinfo else value.isoformat()
    if text.endswith("+00:00"):
        text = text[:-6] + "Z"
    return text


def build_record(
    *,
    # --- Section A: identity and lineage -------------------------------------------
    provenance_id: str,
    tenant_id: str,
    result_id: str,
    job_id: str,
    idempotency_key: str,
    root_job_id: str,
    parent_job_id: str | None = None,
    job_depth: int = 0,
    started_at: datetime,
    finished_at: datetime,
    # --- Section B: input ------------------------------------------------------------
    study_instance_uid: str,
    series_considered: tuple[SeriesVerdict, ...],
    series_consumed: tuple[str, ...],
    instance_uids: tuple[str, ...],
    instance_uid_digest: str,
    pixel_digest: str,
    canonical_geometry: dict[str, Any],
    acquisition: dict[str, Any] | None = None,
    gateway: dict[str, Any] | None = None,
    patient_internal_id: str | None = None,
    study_internal_id: str | None = None,
    instance_manifest_uri: str | None = None,
    # --- Section C: resolution and governance ---------------------------------------
    capability_id: str,
    capability_version: str,
    resolution: dict[str, Any] | None = None,
    governance: dict[str, Any] | None = None,
    clinical_use_mode: str = "research_only",
    # --- Section D: execution ---------------------------------------------------------
    service_id: str,
    service_version: str,
    service_image_digest: str | None = None,
    execution_mode: str = "native",
    models: tuple[ModelRef, ...] = (),
    preprocessing_specs: tuple[PreprocessingSpecRef, ...] = (),
    preprocessing_selftest: dict[str, Any] | None = None,
    operating_threshold: float | None = None,
    threshold_source: str | None = None,
    inference: dict[str, Any] | None = None,
    accelerator: dict[str, Any] | None = None,
    worker_version: str,
    runtime_version: str,
    platform_commit: str | None = None,
    applicability: dict[str, Any] | None = None,
    plausibility: dict[str, Any] | None = None,
    reproducibility_class: str = "numeric_tolerance",
    # --- Section E: evidence -----------------------------------------------------------
    evidence: dict[str, Any] | None = None,
    # --- Section F: output --------------------------------------------------------------
    outputs: tuple[GeneratedObject, ...] = (),
    outputs_omitted: tuple[dict[str, Any], ...] = (),
    child_jobs: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Assemble the `MOS-SAFE-083` document. Validates before returning.

    `sequence_no`, `prev_record_hash` and `record_hash` are DELIBERATELY absent: they are
    assigned by the `result_provenance_chain_trg` BEFORE INSERT trigger (`MOS-SAFE-090`)
    and written back into the stored document, so the exported record is self-verifying
    and the chain cannot be skipped by a writer. A builder that computed them would be
    computing a chain position it cannot know without taking the tenant's lock.

    Raises `ProvenanceIncomplete` rather than returning a record that cannot answer
    `MOS-SAFE-092`'s question.
    """
    record: dict[str, Any] = {
        "provenance_id": provenance_id,
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "result_id": result_id,
        "job_id": job_id,
        "parent_job_id": parent_job_id,
        "root_job_id": root_job_id,
        "job_depth": job_depth,
        "idempotency_key": idempotency_key,
        "started_at": _stamp(started_at),
        "finished_at": _stamp(finished_at),
        "duration_ms": max(0, int((finished_at - started_at).total_seconds() * 1000)),
        "input": {
            "patient_internal_id": patient_internal_id,
            "study_internal_id": study_internal_id,
            "study_instance_uid": study_instance_uid,
            # MOS-SAFE-084: every series triaged, with the rejected ones and their reasons.
            "series_considered": [s.as_json() for s in series_considered],
            # MOS-SAFE-083 section B: "the series actually read by the service".
            "series_consumed": list(series_consumed),
            "instances": {
                "count": len(instance_uids),
                "sha256_of_sorted_sop_uid_list": instance_uid_digest,
                # MOS-STORE-279 re-points this at result_provenance.input_instance_uids,
                # which IS the full list and is the only copy. The URI is the API path
                # that dereferences to it, not a second store.
                "manifest_uri": instance_manifest_uri,
                # Carried inline as well, because MOS-SAFE-092's reproduce tool must
                # "re-read the recorded instance manifest" from the record it was handed.
                "sop_instance_uids": list(instance_uids),
            },
            "pixel_digest": pixel_digest,
            "canonical_geometry": canonical_geometry,
            "acquisition": acquisition or {},
            # MOS-STORE-251: the de-identification policy version is pinned in here.
            "gateway": gateway or {},
        },
        "resolution": {
            "capability_id": capability_id,
            "capability_version": capability_version,
            **(resolution or {}),
        },
        "governance": {
            "clinical_use_mode": clinical_use_mode,
            **(governance or {}),
        },
        "execution": {
            "service_id": service_id,
            "service_version": service_version,
            "service_image_digest": service_image_digest,
            "execution_mode": execution_mode,
            "models": [m.as_json() for m in models],
            "preprocessing_specs": [p.as_json() for p in preprocessing_specs],
            "preprocessing_selftest": preprocessing_selftest or {},
            "operating_threshold": operating_threshold,
            "threshold_source": threshold_source,
            "inference": inference or {},
            "accelerator": accelerator or {},
            "runtime": {
                "worker_version": worker_version,
                "runtime_version": runtime_version,
                "platform_commit": platform_commit,
            },
            "applicability": applicability or {},
            "plausibility": plausibility or {},
            "reproducibility_class": reproducibility_class,
        },
        # MOS-SAFE-016: when the publisher declared no training population the record
        # says so, in the record, so every surface of MOS-SAFE-012 can render the
        # absence rather than render nothing.
        "evidence": {"training_population_declared": False, **(evidence or {})},
        "outputs": [o.as_json() for o in outputs],
        # The other half of section F, and the half that used not to exist: which objects
        # this job could have produced and did not, with a `reason_code` per entry
        # (`medos.writer.identity.OmittedOutput`).
        #
        # `outputs` alone cannot answer it. A job whose capability found nothing to
        # outline writes no SEG, and an `outputs` list holding only an SR is, on its own,
        # indistinguishable from a job whose SEG went missing between the writer and the
        # PACS -- which is the one failure mode `MOS-SAFE-085` exists to make visible.
        # Recording the absence with its reason keeps "not produced, and here is why"
        # separate from "unaccounted for", exactly as `MOS-SVC-011` requires one layer up
        # for a capability that produced no output.
        "outputs_omitted": [dict(o) for o in outputs_omitted],
        # MOS-SAFE-102: a parent MUST record its children's terminal states. Empty
        # throughout 0.1.0-0.3.0, where MOS-SAFE-103's inverse check applies (every job is
        # depth 0), and present so the field is not invented later under pressure.
        "child_jobs": [dict(c) for c in child_jobs],
    }

    violations = validate(record)
    if violations:
        raise ProvenanceIncomplete(violations)
    return record


def validate(record: dict[str, Any]) -> list[str]:
    """Every way this record fails `MOS-SAFE-083`, as a list. Empty means complete.

    Checks the invariants that a column CHECK cannot see because they are relations
    between fields:

      * `input.series_consumed` MUST be a strict subset of the SELECTED entries of
        `input.series_considered` (`MOS-SAFE-083` section B, and chapter 9's acceptance
        criterion 17 checks exactly this);
      * the instance list and its count agree, and the digest is present;
      * at least one preprocessing spec (criterion 14: "at least one element in `models`
        and in `preprocessing_specs`");
      * every output names at least one SOPInstanceUID and a SeriesInstanceUID, because
        an output entry that does not is a record of nothing.
    """
    v: list[str] = []
    inp = record.get("input") or {}

    considered = inp.get("series_considered") or []
    if not considered:
        v.append("input.series_considered is empty (MOS-SAFE-084)")
    selected = {s["series_instance_uid"] for s in considered if s.get("selected")}
    consumed = set(inp.get("series_consumed") or [])
    if not consumed:
        v.append("input.series_consumed is empty; a result consumed something")
    extra = consumed - selected
    if extra:
        v.append(
            f"input.series_consumed has {len(extra)} series not marked selected in "
            "series_considered (MOS-SAFE-083 section B)"
        )
    for s in considered:
        if not s.get("selected") and not s.get("rejection_reason"):
            v.append(
                f"series {s.get('series_instance_uid')} is rejected with no "
                "rejection_reason (MOS-SAFE-084)"
            )

    instances = inp.get("instances") or {}
    uids = instances.get("sop_instance_uids") or []
    if not uids:
        v.append("input.instances.sop_instance_uids is empty (MOS-STORE-279)")
    if instances.get("count") != len(uids):
        v.append("input.instances.count disagrees with the UID list it counts")
    if not instances.get("sha256_of_sorted_sop_uid_list"):
        v.append("input.instances.sha256_of_sorted_sop_uid_list is missing")
    if not inp.get("pixel_digest"):
        v.append("input.pixel_digest is missing (MOS-SAFE-083 section B)")
    if not inp.get("canonical_geometry"):
        v.append("input.canonical_geometry is missing (MOS-SAFE-083 section B)")

    ex = record.get("execution") or {}
    if not ex.get("preprocessing_specs"):
        v.append(
            "execution.preprocessing_specs is empty -- this is the field whose absence "
            "made the old record non-reproducible (MOS-SAFE-083 section D)"
        )
    runtime = ex.get("runtime") or {}
    for key in ("worker_version", "runtime_version"):
        if not runtime.get(key):
            v.append(f"execution.runtime.{key} is missing (CONTRACT.md section 10)")

    for i, o in enumerate(record.get("outputs") or []):
        if not o.get("series_instance_uid"):
            v.append(f"outputs[{i}] has no series_instance_uid")
        if not o.get("sop_instance_uids"):
            v.append(f"outputs[{i}] names no SOPInstanceUID (MOS-SAFE-083 section F)")

    if not record.get("idempotency_key"):
        v.append("idempotency_key is missing (MOS-EXEC-053)")
    return v


def assert_no_phi(
    record: dict[str, Any], *, forbidden_values: frozenset[str] = frozenset()
) -> None:
    """`MOS-SAFE-086`, as an executable check. Raises `ValueError` naming the path.

    Two halves, because chapter 9's acceptance criterion 19 asks for both: a key scan for
    the named PHI attributes, and a value scan against "the tenant's known patient-name
    fixtures", which the caller supplies because this module has no fixtures.
    """
    lowered = {v.strip().casefold() for v in forbidden_values if v and v.strip()}

    def walk(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for k, val in node.items():
                if k.replace("-", "_").casefold() in PHI_FORBIDDEN_KEYS:
                    raise ValueError(
                        f"MOS-SAFE-086: provenance record carries a PHI key at {path}.{k}"
                    )
                walk(val, f"{path}.{k}")
        elif isinstance(node, list):
            for i, val in enumerate(node):
                walk(val, f"{path}[{i}]")
        elif isinstance(node, str) and lowered and node.strip().casefold() in lowered:
            raise ValueError(f"MOS-SAFE-086: provenance record carries a PHI value at {path}")

    walk(record, "$")


# =====================================================================================
# The two questions a record MUST answer on its own.  MOS-SAFE-092.
# =====================================================================================
def consumed_inputs(record: dict[str, Any]) -> dict[str, Any]:
    """"What exactly went in", read from the record and nothing else.

    Exists as a function rather than as a paragraph in a docstring so that
    `tests/integration/test_audit_provenance.py` can call it against a record loaded from
    a JSON file with no database connection open, which is the only honest way to assert
    the record is self-sufficient.
    """
    inp = record["input"]
    return {
        "study_instance_uid": inp["study_instance_uid"],
        "series": list(inp["series_consumed"]),
        "instance_count": inp["instances"]["count"],
        "instance_uids": list(inp["instances"]["sop_instance_uids"]),
        "uid_digest": inp["instances"]["sha256_of_sorted_sop_uid_list"],
        "pixel_digest": inp["pixel_digest"],
        "geometry": inp["canonical_geometry"],
        "rejected_series": [
            {"series_instance_uid": s["series_instance_uid"], "reason": s["rejection_reason"]}
            for s in inp["series_considered"]
            if not s["selected"]
        ],
    }


def produced_outputs(record: dict[str, Any]) -> list[dict[str, Any]]:
    """"What exactly came out, and where it landed", read from the record and nothing else."""
    out: list[dict[str, Any]] = []
    for o in record.get("outputs") or []:
        for sop_uid in o["sop_instance_uids"]:
            out.append(
                {
                    "kind": o["kind"],
                    "sop_class_uid": o["sop_class_uid"],
                    "series_instance_uid": o["series_instance_uid"],
                    "sop_instance_uid": sop_uid,
                    "stored_at": o["destination"]["stow_endpoint"],
                    "qido_verified_at": o["destination"]["qido_verified_at"],
                    "sha256": o["sha256"],
                    "size_bytes": o["size_bytes"],
                }
            )
    return out
