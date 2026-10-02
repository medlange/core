# SPDX-License-Identifier: Apache-2.0
"""`ResultBundle` -> `results` / `result_measurements` / `result_dicom_objects` rows.

The mapping half of `persist_result`, split out of `steps.py` so that the step executor
stays a straight-line pipeline. CONTRACT.md section 1 lists two modules under `worker/`;
this is a third and is REPORTED as a contract addition.

It writes NOTHING. Every function here builds value objects, and `steps.py` hands them to
`repo.complete_job_with_results` inside the single transaction that also performs T8
(CONTRACT.md section 8, MOS-EXEC-057). Keeping the mapping pure is what makes that
transaction short enough to reason about: nothing between the first INSERT and the state
change can raise for a reason that has to do with interpreting a bundle.

No PHI (CONTRACT.md section 11): UIDs, codes, counts, millimetres and digests.

Spec: MOS-IMG-062, MOS-IMG-098, MOS-IMG-119, MOS-SAFE-083, MOS-STORE-273, MOS-STORE-282,
MOS-STORE-286, CONTRACT.md sections 5, 8 and 10.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from medos.core import provenance as prov
from medos.core.bundle import CapabilityOutcome
from medos.core.geometry import CanonicalVolume, SourceGeometry
from medos.core.uids import SOP_CLASS_SEG, SOP_CLASS_SR_COMPREHENSIVE_3D
from medos.db.repo import SLICE_TENANT_ID, DicomObjectRow
from medos.db.repo import _uid_digest as repo_uid_digest
from medos.writer.identity import producing_outcome

if TYPE_CHECKING:  # pragma: no cover - imported for typing only; steps.py imports this
    from medos.worker.steps import PipelineState

__all__ = [
    "dicom_object_rows",
    "seg_frame_count",
    "finding_json",
    "geometry_record",
    "provenance_record",
    "provenance_outputs",
    "provenance_uid_digest",
]


def dicom_object_rows(
    state: PipelineState, outcome: CapabilityOutcome
) -> tuple[DicomObjectRow, ...]:
    """Attach the SEG and the SR to the result that owns the label map.

    `result_dicom_objects.result_id` is a single FK, so one object belongs to one result.
    The SEG obviously belongs to the capability whose segments it renders. The SR is
    job-level -- it carries measurements from every capability -- and is attached to the
    same result rather than duplicated across all of them, because duplicating it would
    put one SOPInstanceUID in several rows and make `mark_dicom_stored` ambiguous.
    REPORTED as a limitation: weeks 3-5 wants a job-level outputs table.

    WHICH result owns them when no capability returned a label map -- the detector that
    found nothing -- is `producing_outcome`: the first outcome in bundle order. The SR
    exists in that case and has to hang off some result, and attaching it to none would
    make `GET /api/v1/results/{id}` report no object for a job that wrote one, which is
    the same silence in a different table.

    Only objects this job actually WROTE get a row. A row for a SEG that was never minted
    would give `mark_dicom_stored` an instance to wait for forever, and would put a
    SOPInstanceUID in `result_dicom_objects` that resolves to nothing in the PACS.
    """
    if state.plan is None or state.bundle is None:
        return ()
    if outcome.capability_id != producing_outcome(state.bundle).capability_id:
        return ()
    plan = state.plan

    def _file_facts(sop_uid: str) -> tuple[str | None, int | None]:
        """sha256 and size of the bytes actually sent, or `(None, None)` on the skip path.

        `None` when `write_dicom` was skipped (MOS-EXEC-059) and no file was produced this
        attempt: the columns are nullable precisely so a reconciled object can say "I did
        not write these bytes" instead of claiming a digest it never computed.
        """
        path = state.object_files.get(sop_uid)
        if path is None or not path.exists():
            return None, None
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest(), len(data)

    seg_digest, seg_size = _file_facts(plan.seg_sop_instance_uid)
    sr_digest, sr_size = _file_facts(plan.sr_sop_instance_uid)
    inputs = {
        "tenant_id": SLICE_TENANT_ID,
        "idempotency_key": plan.identity.idempotency_key,
        "service_id": plan.identity.service_id,
        "service_version": plan.identity.service_version,
        "model_id": plan.identity.model_id,
        "model_version": plan.identity.model_version,
        "uid_space": plan.identity.uid_space,
    }
    rows: list[DicomObjectRow] = []
    if plan.writes_seg:
        rows.append(
            DicomObjectRow(
                object_kind="SEG",
                sop_class_uid=SOP_CLASS_SEG,
                series_instance_uid=plan.seg_series_instance_uid,
                sop_instance_uid=plan.seg_sop_instance_uid,
                series_number=plan.seg_series_number,
                output_index=0,
                # MOS-STORE-286: the exact MOS-IMG-062 tuple, so a retry recomputes the
                # identity instead of re-deriving it from live state.
                derivation_inputs={**inputs, "uid_kind": "seg.instance", "output_index": 0},
                # The SEG's actual multi-frame count -- `omit_empty_frames=True`
                # (MOS-IMG-107) means it is NOT segments x slices, so it is read off the
                # written object rather than computed.
                frame_count=seg_frame_count(state),
                object_digest=seg_digest,
                size_bytes=seg_size,
            )
        )
    if plan.writes_sr:
        rows.append(
            DicomObjectRow(
                object_kind="SR",
                sop_class_uid=SOP_CLASS_SR_COMPREHENSIVE_3D,
                series_instance_uid=plan.sr_series_instance_uid,
                sop_instance_uid=plan.sr_sop_instance_uid,
                series_number=plan.sr_series_number,
                output_index=0,
                derivation_inputs={**inputs, "uid_kind": "sr.instance", "output_index": 0},
                object_digest=sr_digest,
                size_bytes=sr_size,
            )
        )
    return tuple(rows)


def seg_frame_count(state: PipelineState) -> int | None:
    """`NumberOfFrames` of the SEG this attempt wrote, or None on the MOS-EXEC-059 skip."""
    if state.seg is None:
        return None
    frames = getattr(state.seg, "NumberOfFrames", None)
    return int(frames) if frames else None


def finding_json(finding: Any) -> dict[str, Any]:
    return {
        "kind": finding.kind,
        "present": finding.present,
        "score": finding.score,
        "measurements": [
            {
                "name": {
                    "scheme": m.name.scheme,
                    "code": m.name.code,
                    "meaning": m.name.meaning,
                },
                "value": float(m.value),
                "unit": m.unit,
            }
            for m in finding.measurements
        ],
    }


def geometry_record(volume: CanonicalVolume, source: SourceGeometry) -> dict[str, Any]:
    """MOS-STORE-273's spatial record, PHI-free and small enough to live on the row."""
    return {
        "frame_of_reference_uid": volume.frame_of_reference_uid,
        "series_instance_uid": volume.series_instance_uid,
        "shape": list(volume.shape),
        "spacing_mm": [float(s) for s in volume.spacing_mm],
        "origin_lps_mm": [float(s) for s in volume.origin_lps_mm],
        "direction_lps": [float(s) for s in volume.direction_lps],
        "anatomical_code": volume.anatomical_code,
        "spacing_class": volume.spacing_class,
        "tilt_deg": float(volume.tilt_deg),
        "tilt_corrected": bool(volume.tilt_corrected),
        "resampled_from_source": bool(volume.resampled_from_source),
        "builder_version": volume.builder_version,
        "source_pixel_spacing_mm": [float(s) for s in source.pixel_spacing_mm],
        "source_delta_s_mm": float(source.delta_s_mm),
        "computation_geometry": "source",
    }




# =====================================================================================
# The provenance record.  MOS-SAFE-082 / MOS-SAFE-083, sections A-F.
#
# Weeks 1-2 recorded CONTRACT.md section 10's field set on the `results` row. That set is
# a SUBSET of MOS-SAFE-083 sections A and D, and MOS-SAFE-083 names precisely what the
# subset cannot do: "the widened fields -- which series were actually consumed, the
# preprocessing version, the de-identification policy version, the evidence dataset
# version, and where every output object landed -- are what turn a list of version
# strings into a reproducible record."
#
# This function is where the widening happens. It stays PURE -- every value comes off the
# `PipelineState`, the `jobs` row that was read at claim, and the capability object -- so
# that `medos.db.provenance.save` can write it inside the ONE transaction that also
# performs T8, with nothing in that transaction that can raise for a reason to do with
# interpreting a bundle.
# =====================================================================================
def _applicability_record(state: PipelineState) -> dict[str, Any]:
    """`MOS-SAFE-083` section `execution.applicability` -- `{in_envelope, violations[]}`.

    This used to be the literal `{"in_envelope": True, "violations": []}`, which was the
    only honest thing to write when no envelope existed to be inside of. It is no longer
    honest: a study in the MARGINAL zone that a tenant's `flag` policy allowed through is
    OUTSIDE the declared envelope and was permitted anyway, and a provenance record that
    claims `in_envelope: true` for it misstates the evidence behind the number it is
    attached to -- which is the one thing a provenance record exists not to do
    (`MOS-SAFE-085`: "the record must not claim more than was verified").

    Three states, not two:
      * an envelope was declared and the study was `IN`      -> in_envelope true
      * an envelope was declared and the study was `MARGINAL` -> in_envelope FALSE, with
        the violations, and the zone named. (`OUT` never reaches here: the job was
        REJECTED before any result row existed.)
      * no envelope was declared                              -> in_envelope null and
        `declared: false`. Null rather than false: "we did not check" is a different fact
        from "we checked and it was outside", and collapsing them is how an unchecked
        study later reads as a checked one.
    """
    verdict = state.envelope_verdict
    if verdict is None:
        return {"declared": False, "in_envelope": None, "violations": []}
    return {
        "declared": True,
        "in_envelope": verdict.in_envelope,
        "zone": verdict.zone,
        "marginal_policy": verdict.marginal_policy,
        "envelope_version": verdict.envelope.version,
        "envelope_digest": verdict.envelope.digest,
        "violations": verdict.violations(),
    }


def provenance_record(
    *,
    state: PipelineState,
    outcome: CapabilityOutcome,
    capability: Any,
    job: dict[str, Any],
    tenant_id: str,
    result_id: str,
    provenance_id: str,
    worker_version: str,
    runtime_version: str,
    platform_commit: str | None,
    preprocessing_version: str,
    preprocessing_digest: str | None,
    finished_at: datetime,
    gateway_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the MOS-SAFE-083 document for one capability's result.

    `series_considered` comes from `state.series_verdicts`, which `fetch_series` filled
    with the SAME verdict objects it wrote to `job_series`. Reading the rows back would
    have made a second source of truth for MOS-SAFE-084's "the record must show what else
    was on the table", and the two would drift the first time a selector changed.
    """
    volume = state.volume
    source = state.source
    assert volume is not None and source is not None
    series_uid = str(state.series_instance_uid)

    considered = tuple(
        prov.SeriesVerdict(
            series_instance_uid=v.series_instance_uid,
            selected=v.decision == "selected",
            series_number=v.rank,
            # MOS-SAFE-084: a rejected series with no reason is the exact failure this
            # field exists to prevent, so the fallback is a code and never None.
            rejection_reason=(
                None if v.decision == "selected" else (v.reason_code or "unspecified")
            ),
            instance_count=v.instance_count,
            modality=v.modality,
        )
        for v in state.series_verdicts
    ) or (
        # A pipeline that reached a result without recorded verdicts still consumed
        # exactly one series; recording that as the only entry is honest and keeps
        # `validate()`'s subset rule true. Reached only by fixtures that drive
        # `persist_result` directly.
        prov.SeriesVerdict(series_instance_uid=series_uid, selected=True),
    )

    return prov.build_record(
        provenance_id=provenance_id,
        tenant_id=tenant_id,
        result_id=result_id,
        job_id=str(job["public_id"]),
        idempotency_key=str(job["idempotency_key"]),
        root_job_id=str(job["root_job_id"]),
        parent_job_id=str(job["parent_job_id"]) if job.get("parent_job_id") else None,
        job_depth=int(job.get("depth") or 0),
        started_at=state.started_at,
        finished_at=finished_at,
        study_instance_uid=str(job["study_instance_uid"]),
        series_considered=considered,
        series_consumed=(series_uid,),
        instance_uids=tuple(outcome.source_sop_instance_uids),
        instance_uid_digest=provenance_uid_digest(outcome.source_sop_instance_uids),
        # MOS-IMG-036 spells it `sha256:<hex>`; the column and the record keep the hex.
        pixel_digest=volume.pixel_digest.split(":", 1)[-1],
        canonical_geometry=geometry_record(volume, source),
        acquisition={
            "modality": volume.modality,
            "body_part": volume.anatomical_code,
            "kernel": source.convolution_kernel,
            "slice_thickness_mm": (
                float(source.slice_thickness_mm)
                if source.slice_thickness_mm is not None
                else None
            ),
            "projected_slice_spacing_mm": float(source.delta_s_mm),
            "spacing_class": volume.spacing_class,
            "value_units": volume.value_units,
            "rescale_type_assumed": bool(volume.rescale_type_assumed),
            # kvp, contrast_phase, manufacturer and model are MOS-SAFE-083 members this
            # slice does not carry: the builder returns geometry and pixels, not the full
            # acquisition envelope, and synthesising them from a second read of the same
            # instances would be work for a field nothing enforces yet. REPORTED.
            "kvp": None,
            "contrast_phase": None,
            "manufacturer": None,
            "model": None,
        },
        # MOS-STORE-251: the de-identification policy version is pinned INTO provenance.
        # The Gateway owns those values; what it reports is copied verbatim and what it
        # does not report stays null rather than defaulting to something reassuring.
        gateway=dict(gateway_summary or {}),
        capability_id=outcome.capability_id,
        capability_version=str(capability.version),
        resolution={
            # MOS-EXEC-006 / MOS-EXEC-086: capability resolution is 0.3.0 and this slice
            # carries the denormalised service identity instead. `reason` says which,
            # rather than leaving the member absent and unexplained.
            "registry_snapshot_id": None,
            "candidates": [],
            "reason": "job_pin",
        },
        governance={
            "deployment_id": None,
            "deployment_environment": None,
            "deployment_state_at_execution": None,
            "deployment_role_at_execution": None,
            "jurisdiction": None,
            "regulatory_status_at_execution": None,
            # MOS-SAFE-003: the manufacturer of a clinical output is the publisher of the
            # ServiceVersion. There is no registry in this slice, so the platform is the
            # publisher and says so, rather than leaving the chain unresolvable.
            "legal_manufacturer": {
                "name": "MedicalOS (unregistered development build)",
                "declared": False,
            },
            "intended_use_digest": None,
            "clinical_promotion_audit_id": None,
            "policy_decision_ids": [],
            "review": {"result_review_id": None, "review_status_at_write": "UNREVIEWED"},
        },
        clinical_use_mode=str(job["clinical_use_mode"]),
        service_id=str(job["service_id"]),
        service_version=str(job["service_version"]),
        service_image_digest=None,
        execution_mode="native",
        models=(
            prov.ModelRef(
                role="primary",
                model_id=outcome.capability_id,
                version=str(capability.version),
                artifact_digest=None,
            ),
        ),
        preprocessing_specs=(
            prov.PreprocessingSpecRef(
                id="medos.canonical_volume",
                version=preprocessing_version,
                digest=preprocessing_digest,
            ),
        ),
        preprocessing_selftest={},
        operating_threshold=None,
        threshold_source=None,
        inference={"engine": "in_process", "backend": "numpy", "engine_build_digest": None},
        accelerator={"gpu_model": None, "driver": None, "cuda": None, "trt": None},
        worker_version=worker_version,
        runtime_version=runtime_version,
        platform_commit=platform_commit,
        applicability=_applicability_record(state),
        plausibility={"passed": True, "flags": []},
        # MOS-SAFE-092: the in-process numpy path is deterministic on one machine but is
        # not bitwise across platforms, and MOS-SAFE-093 makes `not_reproducible` fatal at
        # the clinical gate. `numeric_tolerance` is the honest claim for this slice.
        reproducibility_class="numeric_tolerance",
        evidence={
            "validation_report_id": None,
            "validation_report_digest": None,
            "evaluation_run_id": None,
            "dataset_version_id": None,
            "dataset_version_digest": None,
            "dataset_split_id": None,
            "annotation_set_id": None,
            "acceptance_criteria_id": None,
            "acceptance_criteria_version": None,
            # MOS-SAFE-016: the absence is surfaced, not papered over.
            "training_population_declared": False,
        },
        outputs=provenance_outputs(state),
        outputs_omitted=(
            () if state.plan is None else tuple(o.to_dict() for o in state.plan.omitted_outputs)
        ),
    )


def provenance_outputs(state: PipelineState) -> tuple[prov.GeneratedObject, ...]:
    """`MOS-SAFE-083` section F for the SEG and the SR this attempt wrote.

    `qido_verified_at` comes from `state.qido_verified_at`, which `store_dicom` sets only
    after `DicomWebGateway.assert_stored` has re-read the derived series over QIDO-RS and
    found the expected UID set (MOS-IMG-152). `MOS-SAFE-085` is explicit that a STOW-RS
    200 is not proof of landing, so the STOW status and the verification instant are two
    different members and the second stays null when no re-read happened.
    """
    if state.plan is None:
        return ()
    plan = state.plan

    def facts(sop_uid: str) -> tuple[str | None, int | None]:
        path = state.object_files.get(sop_uid)
        if path is None or not path.exists():
            return None, None
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest(), len(data)

    seg_digest, seg_size = facts(plan.seg_sop_instance_uid)
    sr_digest, sr_size = facts(plan.sr_sop_instance_uid)
    # Only the objects this job wrote. `MOS-SAFE-083` section F is the record of what was
    # GENERATED, so listing a SEG that was never minted would put a
    # `qido_verified_at: null` output in the provenance of a job that is complete and
    # correct -- indistinguishable from an object that was written and then lost.
    # `outputs_omitted` in the execution block carries the absence instead, with its
    # reason, which is the difference between "not produced" and "unaccounted for".
    outputs: list[prov.GeneratedObject] = []
    if plan.writes_seg:
        outputs.append(
            prov.GeneratedObject(
                output_index=0,
                kind="SEG",
                sop_class_uid=SOP_CLASS_SEG,
                series_instance_uid=plan.seg_series_instance_uid,
                sop_instance_uids=(plan.seg_sop_instance_uid,),
                series_number=plan.seg_series_number,
                stow_endpoint=state.stow_endpoint,
                stow_http_status=state.stow_http_status,
                qido_verified_at=state.qido_verified_at,
                instance_count_verified=state.verified_counts.get(
                    plan.seg_series_instance_uid
                ),
                sha256=seg_digest,
                size_bytes=seg_size,
                series_description=getattr(state.seg, "SeriesDescription", None),
            )
        )
    if plan.writes_sr:
        outputs.append(
            prov.GeneratedObject(
                output_index=0,
                kind="SR",
                sop_class_uid=SOP_CLASS_SR_COMPREHENSIVE_3D,
                series_instance_uid=plan.sr_series_instance_uid,
                sop_instance_uids=(plan.sr_sop_instance_uid,),
                series_number=plan.sr_series_number,
                stow_endpoint=state.stow_endpoint,
                stow_http_status=state.stow_http_status,
                qido_verified_at=state.qido_verified_at,
                instance_count_verified=state.verified_counts.get(
                    plan.sr_series_instance_uid
                ),
                sha256=sr_digest,
                size_bytes=sr_size,
                series_description=getattr(state.sr, "SeriesDescription", None),
            )
        )
    return tuple(outputs)


def provenance_uid_digest(sop_instance_uids: Sequence[str]) -> str:
    """`MOS-SAFE-083` section B's `sha256_of_sorted_sop_uid_list`.

    Delegates to `medos.db.repo._uid_digest` rather than reimplementing it, because the
    record and the `results.input_uid_digest` column describe the SAME set: a reader
    comparing the two is checking the writer, not the algorithm, and two implementations
    would turn that check into a test of whether they still agree.
    """
    return repo_uid_digest(sop_instance_uids)
