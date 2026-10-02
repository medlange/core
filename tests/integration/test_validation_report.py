# SPDX-License-Identifier: Apache-2.0
"""The signed, offline-verifiable `ValidationReport`. Migration 0009, chapter 7 §7.12.

WHAT THIS FILE IS TRYING TO CATCH
---------------------------------
docs/spec/15-delivery.md §15.2.5 names one risk for this block: "whether evidence is
portable rather than site-regenerated -- proved by verifying a `ValidationReport` on a
machine with no network access to the platform." The 0.2.0 gate spells the check
`report-offline-verify`. Chapter 18 records the same gap from the conformance side and
`MOS-EVID-121` states it in a sentence: signing the model artifact but not the claim about
it is backwards.

So the assertions below are, in order of how much they matter:

  1. A bundle verifies with NO ROUTE TO THE PLATFORM. Proved by running the verifier in a
     subprocess whose `socket` module raises on every call, including name resolution --
     and by first proving, in that same interpreter, that resolving the API host fails.
     A container with no network would prove less: it could not show that the verifier
     never even ATTEMPTS a connection, which is what `MOS-EVID-124` actually requires.
  2. Tampering with one byte fails verification, at the right check, with the right exit
     code. Chapter 7 acceptance check 34 names four of them and they are each a test here.
  3. The signature covers the CLAIM, not just the artifact: a re-signed report with a
     doctored aggregate still fails, because a signature makes a document authentic and
     not true.
  4. Sealing is a DATABASE property (`MOS-EVID-013`): every assertion about immutability
     goes through SQL as `medicalos_app`, not through the repository.
  5. Revocation preserves the bytes (`MOS-EVID-126`): a revoked report still verifies
     offline, which is what makes "withdrawn" an auditable fact rather than a hole.

ONE TAUTOLOGY, DECLARED
-----------------------
The happy-path fixture computes its `aggregates` with the verifier's own recomputation, so
check 5 passing on an untampered bundle proves little by itself. That is why
`test_aggregates_are_independently_correct` recomputes one figure by hand from the fixture
definition, and why every other check-5 assertion here is a TAMPER assertion.

SKIP DISCIPLINE: `tests/_support/skips.py` only, through `conftest.pg_dsn`. No raw
`pytest.skip` appears in this file.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from medos.db.conn import apply_schema
from medos.db.migrate import discover
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.evidence import dsse, verify
from medos.evidence.bundle import (
    CASE_SCORE_COLUMNS,
    Bundle,
    BundleError,
    build_bundle,
    checksums_text,
    read_bundle,
    write_csv,
)
from medos.evidence.digest import sha256_of
from medos.evidence.report import (
    DISCLAIMER,
    FORBIDDEN_CLAIMS,
    SigningRefused,
    forbidden_claim_hits,
    report_bytes,
    report_digest,
    validate_report,
)
from medos.evidence.reports import (
    ReportRefused,
    build_report_document,
    export_bundle,
    get_report,
    issue_report,
    new_export_salt,
    rekeyed_case_rows,
    revoke_report,
    revoke_reports_citing_runs,
)
from medos.evidence.store import InMemoryManifestStore
from medos.sdk.canonical import canonical_bytes
from psycopg.rows import dict_row

from tests._support.roots import REPO_ROOT, child_pythonpath

#: BOTH import roots, computed in one place. `medos` is `medos/medos/` now, so a
#: child given only the repository root raises ModuleNotFoundError -- and reports it
#: as whatever this test was measuring. See `tests/_support/roots.py`.
BUCKET = "medos-evidence"
OPERATOR = "11111111-1111-1111-1111-111111111111"

# Chapter 8 §8.3.2's two permissions. `MOS-SEC-042` forbids a seeded role holding both,
# and `evidence_scientist` -- the evaluation runner -- holds the first and not the second.
RUNNER_SCOPE = ("validation_report.create", "validation_report.export", "evaluation.run")
APPROVER_SCOPE = ("validation_report.approve", "validation_report.read")

# Source values that MUST NOT appear anywhere in an exported bundle (MOS-EVID-116).
SOURCE_STUDY_UID = "1.2.826.0.1.3680043.10.1.77.4001"
SOURCE_SERIES_UID = "1.2.826.0.1.3680043.10.1.77.4001.2"
SOURCE_PATIENT_ID = "MRN0092231"
SOURCE_PATIENT_NAME = "NOVAKOVA^JANA"
SOURCE_ACCESSION = "ACC77120045"
SOURCE_BIRTH_DATE = "19631104"


# =====================================================================================
# The cohort fixture. A pleural-effusion acceptance cohort in the shape of §7.8.2.
# =====================================================================================
def _cases() -> list[dict[str, Any]]:
    """24 cases over 18 patients: 14 reference-positive, 10 reference-empty.

    Two patients contribute two studies each. That is not decoration: `MOS-EVID-057`
    requires the interval to be computed over PATIENT clusters, and a cohort where every
    patient contributes exactly one case cannot tell a clustered bootstrap from a case-level
    one -- so a regression to the wrong resampler would pass silently.
    """
    out: list[dict[str, Any]] = []
    for i in range(14):  # reference-positive
        patient = i if i >= 2 else 0 if i == 0 else 1
        gt = 40000 + 5000 * i
        inter = int(gt * (0.74 + 0.012 * (i % 7)))
        pred = int(gt * (1.04 + 0.01 * (i % 5)))
        out.append(
            {
                "case": i,
                "patient": patient if i >= 2 else i,
                "gt_voxels": gt,
                "pred_voxels": pred,
                "intersection_voxels": inter,
                "gt_volume_ml": round(gt * 0.0016, 4),
                "pred_volume_ml": round(pred * 0.0016, 4),
                "slice_thickness_mm": 1.25 if i % 3 else 3.5,
                "sensitivity_hit": 0.0 if i in (4, 11) else 1.0,
            }
        )
    # Two extra studies for patients 0 and 1 -- the same patients as cases 0 and 1.
    for j, patient in enumerate((0, 1)):
        gt = 62000 + 3000 * j
        out.append(
            {
                "case": 14 + j,
                "patient": patient,
                "gt_voxels": gt,
                "pred_voxels": int(gt * 1.03),
                "intersection_voxels": int(gt * 0.79),
                "gt_volume_ml": round(gt * 0.0016, 4),
                "pred_volume_ml": round(gt * 1.03 * 0.0016, 4),
                "slice_thickness_mm": 1.25,
                "sensitivity_hit": 1.0,
            }
        )
    for k in range(8):  # reference-empty
        pred = 0 if k < 6 else 9000
        out.append(
            {
                "case": 16 + k,
                "patient": 20 + k,
                "gt_voxels": 0,
                "pred_voxels": pred,
                "intersection_voxels": 0,
                "gt_volume_ml": 0.0,
                "pred_volume_ml": round(pred * 0.0016, 4),
                "slice_thickness_mm": 1.25 if k % 2 else 3.5,
                "sensitivity_hit": None,
                "specificity_hit": 1.0 if pred == 0 else 0.0,
            }
        )
    return out


def _case_metric_rows() -> list[dict[str, Any]]:
    """`evaluation_case_metrics` rows, in the tenant's own key space. §7.7.2."""
    rows: list[dict[str, Any]] = []
    for c in _cases():
        case_key = f"{SOURCE_STUDY_UID}.{c['case']}"
        patient_key = f"pk_tenantside{c['patient']:04d}"
        strata = {
            "reference_volume_ml": c["gt_volume_ml"],
            "slice_thickness_mm": c["slice_thickness_mm"],
            "pixel_spacing_mm_max": 0.703125,
            "convolution_kernel_class": "soft",
            "manufacturer": "SIEMENS",
            "contrast_phase": "non_contrast",
            "patient_age_years": 55 + (c["patient"] % 30),
            "patient_sex": "F" if c["patient"] % 2 else "M",
        }
        base = {
            "case_key": case_key,
            "patient_key": patient_key,
            "gt_voxels": c["gt_voxels"],
            "pred_voxels": c["pred_voxels"],
            "intersection_voxels": c["intersection_voxels"],
            "gt_volume_ml": c["gt_volume_ml"],
            "pred_volume_ml": c["pred_volume_ml"],
            "strata": strata,
        }
        empty = c["gt_voxels"] == 0
        value = (
            None
            if empty
            else 2.0 * c["intersection_voxels"] / (c["gt_voxels"] + c["pred_voxels"])
        )
        rows.append(
            {
                **base,
                "metric": "dice_mean_per_case",
                "value": value,
                # MOS-EVID-051: the empty-GT case leaves the aggregate and is counted in
                # the empty-GT block instead. MOS-EVID-068: the counts are persisted anyway.
                "undefined_reason": "empty_ground_truth" if empty else None,
                "eligible": not empty,
            }
        )
        if c.get("sensitivity_hit") is not None:
            rows.append(
                {
                    **base,
                    "metric": "sensitivity",
                    "value": c["sensitivity_hit"],
                    "undefined_reason": None,
                    "eligible": True,
                }
            )
        if c.get("specificity_hit") is not None:
            rows.append(
                {
                    **base,
                    "metric": "specificity",
                    "value": c["specificity_hit"],
                    "undefined_reason": None,
                    "eligible": True,
                }
            )
    rows.sort(key=lambda r: (r["case_key"], r["metric"]))
    return rows


def _criteria_doc() -> dict[str, Any]:
    """§7.8.2's document, at the bar this fixture cohort actually clears.

    The values are lowered from the chapter's worked example because the example is sized
    for n >= 300 and this cohort is 24 cases; the STRUCTURE -- `bound: ci_lower_95`
    everywhere (MOS-EVID-077), an empty-GT companion for the Dice criterion
    (MOS-EVID-052), an advisory criterion that must not gate (MOS-EVID-084), a regression
    criterion that cannot be evaluated offline -- is the chapter's exactly.
    """
    return {
        "apiVersion": "medicalos.io/v1",
        "kind": "AcceptanceCriteria",
        "metadata": {
            "capability_id": "pleural_effusion",
            "version": 3,
            "effective_from": "2026-04-01T00:00:00Z",
            "authored_by": "clinical-council@medicalos.example",
        },
        "spec": {
            "combine": "all_of",
            "fp_volume_threshold_ml": 10.0,
            "case_definition": {
                "positive_if": {"reference_volume_ml": {"op": ">=", "value": 10.0}}
            },
            "strata": [
                {"id": "all", "selector": {}},
                {
                    "id": "gt_positive",
                    "selector": {"reference_volume_ml": {"op": ">=", "value": 10.0}},
                },
                {
                    "id": "gt_empty",
                    "selector": {"reference_volume_ml": {"op": "<", "value": 10.0}},
                },
                {
                    "id": "thick_slice",
                    "selector": {"slice_thickness_mm": {"op": ">", "value": 3.0}},
                },
            ],
            "absolute": [
                {
                    "id": "sens_all",
                    "metric": "sensitivity",
                    "stratum": "all",
                    "operating_threshold": {
                        "name": "effusion_probability",
                        "value": 0.50,
                    },
                    "bound": "ci_lower_95",
                    "op": ">=",
                    "value": 0.60,
                    "min_cases": 12,
                    "min_patients": 10,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
                {
                    "id": "dice_positive",
                    "metric": "dice_mean_per_case",
                    "stratum": "gt_positive",
                    "bound": "ci_lower_95",
                    "op": ">=",
                    "value": 0.60,
                    "min_cases": 12,
                    "min_patients": 10,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
                {
                    # NO `operating_threshold`, exactly as section 7.8.2 writes it: the
                    # false-positive volume bar is `fp_volume_threshold_ml`, declared once
                    # for the document by MOS-EVID-053 and recorded on the run, not an
                    # operating point per MOS-EVID-055 (which names four other metrics).
                    "id": "fp_on_negatives",
                    "metric": "empty_gt_false_positive_rate",
                    "stratum": "gt_empty",
                    "bound": "ci_upper_95",
                    "op": "<=",
                    "value": 0.90,
                    "min_cases": 6,
                    "min_patients": 6,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                },
                {
                    # MOS-EVID-084: reported, never gating. Set deliberately above what the
                    # cohort achieves so that a verdict of PASS proves the advisory
                    # criterion was excluded from the combination rather than passed.
                    "id": "dice_thick_slice",
                    "metric": "dice_mean_per_case",
                    "stratum": "thick_slice",
                    "bound": "ci_lower_95",
                    "op": ">=",
                    "value": 0.99,
                    "min_cases": 3,
                    "min_patients": 3,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "advisory",
                },
            ],
            "regression": [
                {
                    "id": "ni_dice_positive",
                    "metric": "dice_mean_per_case",
                    "stratum": "gt_positive",
                    "margin": 0.02,
                    "bound": "ci_lower_95",
                    "min_cases": 12,
                    "min_patients": 10,
                    "on_insufficient_cases": "indeterminate",
                    "severity": "blocking",
                }
            ],
        },
    }


def _envelope_doc() -> dict[str, Any]:
    return {
        "apiVersion": "medicalos.io/v1",
        "kind": "ApplicabilityEnvelope",
        "metadata": {
            "subject_kind": "service_version",
            "subject_id": "pulmoai.effusion",
            "subject_version": "2.1.0",
            "version": 4,
            "derivation": "percentile_1_99",
        },
        "spec": {
            "constraints": [
                {"attribute": "slice_thickness_mm", "type": "range", "min": 0.625,
                 "max": 3.0, "marginal_max": 5.0},
                {"attribute": "convolution_kernel_class", "type": "enum_in",
                 "values": ["soft", "standard"], "marginal_values": ["sharp"]},
            ],
            "marginal_policy_default": "flag",
        },
    }


def _plausibility_doc() -> dict[str, Any]:
    return {
        "apiVersion": "medicalos.io/v1",
        "kind": "PlausibilityRuleSet",
        "metadata": {"capability_id": "pleural_effusion", "version": 2},
        "spec": {
            "rules": [
                {"id": "pl_volume_range", "type": "volume_range", "target": "effusion",
                 "min_ml": 1.0, "max_ml": 4000.0, "on_violation": "fail"},
                {"id": "pl_components", "type": "component_count", "target": "effusion",
                 "min_component_ml": 5.0, "max_components": 4, "on_violation": "warn"},
            ]
        },
    }


def _manifest_lines(
    case_map: dict[str, str], patient_of_case: dict[str, str]
) -> bytes:
    """A pseudonymised dataset manifest. MOS-EVID-009's JSONL, MOS-EVID-116's keys.

    `sop_class_uid` stays: it is a DICOM constant identifying the object TYPE and says
    nothing about a patient. That is why the PHI scan matches against a list of known
    source values rather than against a UID-shaped regex -- the regex would flag this line
    and be switched off within a week.
    """
    lines = []
    for source_case, pseudo in sorted(case_map.items(), key=lambda kv: kv[1]):
        lines.append(
            canonical_bytes(
                {
                    "case_key": pseudo,
                    "patient_key": patient_of_case[source_case],
                    "modality": "CT",
                    "sop_class_uid": "1.2.840.10008.5.1.4.1.1.2",
                    "instance_count": 240,
                }
            )
            + b"\n"
        )
    return b"".join(lines)


@pytest.fixture(scope="session")
def publisher_key() -> tuple[bytes, bytes]:
    return dsse.generate_keypair()


@pytest.fixture(scope="session")
def countersign_key() -> tuple[bytes, bytes]:
    return dsse.generate_keypair()


@pytest.fixture(scope="session")
def trust_bundle_pem(publisher_key, countersign_key) -> str:
    return dsse.public_key_pem(
        publisher_key[1], "PulmoAI s.r.o. release key"
    ) + dsse.public_key_pem(countersign_key[1], "Site operator countersignature key")


@pytest.fixture(scope="session")
def trusted(trust_bundle_pem) -> list[dsse.TrustedKey]:
    return dsse.parse_trust_bundle(trust_bundle_pem)


def _make_bundle(
    publisher_key: tuple[bytes, bytes],
    *,
    kind: str = "vendor_evidence",
    extra_signers: list[tuple[bytes, bytes]] | None = None,
    issued_at: datetime | None = None,
) -> tuple[Bundle, dict[str, Any], dict[str, Any]]:
    """Build a complete, signed bundle. Returns `(bundle, report, criteria_doc)`.

    The aggregate VALUES come from the verifier's recomputation. That makes the happy-path
    check-5 assertion circular, which is declared in the module docstring and paid for by
    `test_aggregates_are_independently_correct`.
    """
    criteria = _criteria_doc()
    envelope = _envelope_doc()
    plausibility = _plausibility_doc()
    salt, salt_id = new_export_salt()
    csv_rows, patient_map, case_map = rekeyed_case_rows(
        _case_metric_rows(), report_salt=salt
    )
    score_rows = [
        [case_map[k], patient_map[p], "", "", "[]"]
        for k, p in sorted({(r["case_key"], r["patient_key"]) for r in _case_metric_rows()})
    ]
    from medos.evidence.bundle import CASE_METRIC_COLUMNS  # noqa: PLC0415

    case_metrics_csv = write_csv(CASE_METRIC_COLUMNS, csv_rows)
    case_scores_csv = write_csv(CASE_SCORE_COLUMNS, score_rows)
    patient_of_case = {
        r["case_key"]: patient_map[r["patient_key"]] for r in _case_metric_rows()
    }
    dataset_manifest = _manifest_lines(case_map, patient_of_case)
    split_manifest = (
        canonical_bytes({"partition": "test", "n_patients": len(patient_map)})
        + b"\n"
    )
    annotation_manifest = canonical_bytes({"consensus_rule": "majority_at_least_2"}) + b"\n"
    leakage = {"L1": "pass", "L2": "pass", "L3": "pass", "L4": "pass", "L5": "pass",
               "waivers": []}

    # Recompute the aggregates from the exported CSV, through the verifier's own path, so
    # that what the report states is exactly what the per-case rows support.
    rows = _rows_from_csv(case_metrics_csv)
    conventions = {
        "dice_aggregation": "mean_of_per_case",
        "empty_gt_policy": "exclude_and_report_separately",
        "fp_volume_threshold_ml": 10.0,
        "ci_method": "percentile_cluster_bootstrap",
        "bootstrap_b": 400,  # 2000 in production; 400 keeps this suite under a second
        "bootstrap_seed": 20260101,
        "metric_registry_version": 1,
    }
    strata = {s["id"]: s["selector"] for s in criteria["spec"]["strata"]}
    wanted = [
        ("sensitivity", "all", {"name": "effusion_probability", "value": 0.50}),
        ("dice_mean_per_case", "gt_positive", None),
        ("dice_pooled", "gt_positive", None),
        ("empty_gt_false_positive_rate", "gt_empty", None),
        ("empty_gt_case_count", "gt_empty", None),
        ("dice_mean_per_case", "thick_slice", None),
    ]
    aggregates = []
    for metric, stratum, op in wanted:
        subset = [r for r in rows if verify._selector_matches(r.strata, strata[stratum])]
        value, lo, hi, n, n_pat = verify._recompute(metric, subset, conventions=conventions)
        entry = {
            "metric": metric, "stratum": stratum, "value": value,
            "ci_low": lo, "ci_high": hi, "n": n, "n_patients": n_pat,
        }
        if op is not None:
            entry["operating_point"] = op
        aggregates.append(entry)

    recomputed = {
        (a["metric"], a["stratum"]): (
            a["value"], a["ci_low"], a["ci_high"], a["n"], a["n_patients"]
        )
        for a in aggregates
    }
    thresholds = {
        "effusion_probability": {"value": 0.50, "selected_on": "tune"},
        "fp_volume_threshold_ml": {"value": 10.0, "selected_on": "tune"},
    }
    criteria_results = []
    for criterion in criteria["spec"]["absolute"]:
        status, reason = verify._evaluate_absolute(criterion, recomputed, thresholds)
        agg = recomputed.get((criterion["metric"], criterion["stratum"]))
        observed = (
            {"point": agg[0], "ci_lower_95": agg[1], "ci_upper_95": agg[2]}[criterion["bound"]]
            if agg is not None
            else None
        )
        entry = {
            "id": criterion["id"], "status": status,
            "severity": criterion.get("severity", "blocking"),
            "metric": criterion["metric"], "stratum": criterion["stratum"],
            "bound": criterion["bound"], "op": criterion["op"],
            "required": criterion["value"],
        }
        if observed is not None:
            entry["observed"] = observed
            entry["n"], entry["n_patients"] = agg[3], agg[4]
        if reason:
            entry["reason"] = reason
        criteria_results.append(entry)
    for criterion in criteria["spec"]["regression"]:
        criteria_results.append(
            {
                "id": criterion["id"], "status": "SKIPPED",
                "severity": criterion.get("severity", "blocking"),
                "reason": "no_incumbent_first_deployment",
            }
        )

    artifacts = {
        "case_metrics_csv_digest": sha256_of(case_metrics_csv),
        "case_scores_csv_digest": sha256_of(case_scores_csv),
        "dataset_manifest_digest": sha256_of(dataset_manifest),
        "split_manifest_digest": sha256_of(split_manifest),
        "annotation_manifest_digest": sha256_of(annotation_manifest),
        "leakage_report_digest": sha256_of(canonical_bytes(leakage)),
    }
    report = build_report_document(
        kind=kind,
        report_version=1,
        subject={
            "kind": "service_version",
            "id": "pulmoai.effusion",
            "version": "2.1.0",
            "image_digest": "sha256:" + "5c" * 32,
            "legal_manufacturer": {"name": "PulmoAI s.r.o.", "id": "CZ-28471902"},
            "internal_pipeline_digest_attestation": "vendor_asserted",
        },
        capability={
            "id": "pleural_effusion",
            "criteria_version": 3,
            "criteria_digest": sha256_of(canonical_bytes(criteria)),
        },
        cohort={
            "dataset_version_id": "dsv_01JAY7N4K2ZP8QVCM3RXTD6WEB",
            "dataset_version_digest": "sha256:" + "3f" * 32,
            "dataset_name": "PulmoAI pleural effusion evidence cohort",
            "source_id": "pulmoai/multisite-2025",
            "licence_spdx": None,
            "deidentification_status": "public_deidentified",
            "deid_policy_id": "ps315-basic+pixel-ocr/v4",
            "split_id": "spl_01JAY8P0R5T3XJ7NCB2VQM4KZD",
            "split_digest": "sha256:" + "c5" * 32,
            "partition": "test",
            "n_patients": len(patient_map),
            "n_series": len(case_map),
            "annotation_set_id": "ann_01JAY9Q1S6U4YK8PDC3WRN5LAF",
            "annotation_digest": "sha256:" + "9d" * 32,
            "annotation_consensus_rule": "majority_at_least_2",
            "annotation_reader_count": 3,
            "reference_of_record": True,
            "leakage_report": leakage,
        },
        evaluation_run={
            "id": "evr_01JB4Q8T5XN7M2VDKC3PZR9HAE",
            "code_commit": "7e41b2c8a95d3f06b18e4c7a2d905f3b6c81e024",
            "code_dirty": False,
            "inference_backend": {"kind": "sealed_container", "version": "2.1.0"},
            "accelerator": {"gpu_model": "NVIDIA A10", "driver": "550.90.07"},
            "operating_thresholds": thresholds,
            "metric_conventions": conventions,
            "run_digest": "sha256:" + "04" * 32,
        },
        aggregates=aggregates,
        criteria_results=criteria_results,
        approver={
            "name": "Dr. Jana Novakova",
            "role": "head_of_clinical_affairs",
            "organisation": "PulmoAI s.r.o.",
            "statement": "I have reviewed the cohort composition, the reference standard "
            "and the measured results, and I approve publication of this evidence for the "
            "declared applicability envelope.",
            "approved_at": "2026-05-12T08:52:31Z",
            "identity_assurance": "oidc:pulmoai.example/sub/8f21c4",
        },
        applicability_envelope={
            "version": 4, "digest": sha256_of(canonical_bytes(envelope))
        },
        plausibility={
            "rule_set_version": 2,
            "rule_set_digest": sha256_of(canonical_bytes(plausibility)),
            "fire_rates": {"pl_components": 0.012, "pl_volume_range": 0.0},
        },
        engineering={"p95_latency_ms": 41200, "peak_gpu_memory_mib": 9840,
                     "result_schema_valid_rate": 1.0},
        artifacts=artifacts,
        patient_key_scheme={"algorithm": "hmac-sha256-b32-10", "report_salt_id": salt_id},
        issued_at=issued_at or datetime(2026, 5, 12, 9, 14, 7, tzinfo=UTC),
        cites=(
            {"vendor_report_digest": "sha256:" + "ab" * 32}
            if kind == "site_acceptance"
            else None
        ),
    )
    signers = [publisher_key, *(extra_signers or [])]
    bundle, _, _ = export_bundle(
        report=report,
        signers=signers,
        principal_scope=APPROVER_SCOPE,
        criteria_doc=criteria,
        applicability_envelope_doc=envelope,
        plausibility_doc=plausibility,
        case_metric_rows=csv_rows,
        case_score_rows=score_rows,
        dataset_manifest=dataset_manifest,
        split_manifest=split_manifest,
        annotation_manifest=annotation_manifest,
        leakage_report=leakage,
        forbidden_source_values=[
            SOURCE_STUDY_UID, SOURCE_SERIES_UID, SOURCE_PATIENT_ID,
            SOURCE_PATIENT_NAME, SOURCE_ACCESSION, SOURCE_BIRTH_DATE,
        ],
    )
    return bundle, report, criteria


def _rows_from_csv(data: bytes) -> list[Any]:
    tmp = Bundle("vr_x", {"case_metrics.csv": data}, b"")
    return verify._load_rows(tmp)


@pytest.fixture(scope="session")
def signed_bundle(publisher_key) -> tuple[Bundle, dict[str, Any], dict[str, Any]]:
    return _make_bundle(publisher_key)


def _repack(bundle: Bundle, members: dict[str, bytes]) -> Bundle:
    """Rebuild an archive from edited members. The tamper primitive for this file."""
    return build_bundle(bundle.report_id, members)


def _resign(report: dict[str, Any], key: tuple[bytes, bytes]) -> tuple[bytes, bytes]:
    payload = report_bytes(report)
    env = dsse.build_envelope(payload, [key])
    return payload, canonical_bytes(env)


# =====================================================================================
# 1. The crypto. MOS-EVID-118, MOS-EVID-120.
# =====================================================================================
# RFC 8032 §7.1. The three published Ed25519 vectors, verbatim.
RFC8032_VECTORS = (
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
     "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a3"
     "3bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
     "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15"
     "996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16"
     "f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
)


@pytest.mark.parametrize(("sk", "pk", "msg", "sig"), RFC8032_VECTORS)
def test_both_ed25519_backends_match_rfc8032(sk: str, pk: str, msg: str, sig: str) -> None:
    """The library backend and the standard-library fallback agree with the RFC.

    The fallback exists so `medicalos-verify` runs on a machine where installing a wheel is
    a change-control ticket (MOS-EVID-122's "no MedicalOS instance to verify"). A second
    implementation is only safe if it is pinned to the published vectors, so it is.
    """
    public, message, signature = bytes.fromhex(pk), bytes.fromhex(msg), bytes.fromhex(sig)
    assert dsse.verify_raw(public, signature, message) is True
    assert dsse._verify_reference(public, signature, message) is True
    assert dsse.sign(bytes.fromhex(sk), message).hex() == sig
    flipped = bytearray(signature)
    flipped[0] ^= 0x01
    assert dsse.verify_raw(public, bytes(flipped), message) is False
    assert dsse._verify_reference(public, bytes(flipped), message) is False


def test_backends_agree_on_fresh_keys(publisher_key) -> None:
    seed, pub = publisher_key
    for message in (b"", b"a", b"\x00" * 64, b"the claim, not the artifact"):
        sig = dsse.sign(seed, message)
        assert dsse.verify_raw(pub, sig, message)
        assert dsse._verify_reference(pub, sig, message)
        assert not dsse._verify_reference(pub, sig, message + b"!")


def test_pae_is_the_published_encoding() -> None:
    """MOS-EVID-118's PAE, byte for byte.

    The lengths are what make the encoding unambiguous: without them a payload type ending
    in a space and a payload beginning with one are indistinguishable from the other split,
    and one signature would cover both readings.
    """
    assert dsse.pae("t", b"body") == b"DSSEv1 1 t 4 body"
    assert dsse.pae(dsse.PAYLOAD_TYPE, b"") == (
        b"DSSEv1 " + str(len(dsse.PAYLOAD_TYPE)).encode() + b" "
        + dsse.PAYLOAD_TYPE.encode() + b" 0 "
    )


def test_countersignature_is_reported_per_key(publisher_key, countersign_key, trusted) -> None:
    """MOS-EVID-120: which signature verified against which key, not merely "valid"."""
    env = dsse.build_envelope(b"payload", [publisher_key, countersign_key])
    result = dsse.verify_envelope(env, trusted)
    assert [r.verified for r in result.results] == [True, True]
    assert {r.trusted_key_name for r in result.results} == {
        "PulmoAI s.r.o. release key", "Site operator countersignature key"
    }
    # One trusted key only: the other signature is reported as unverified, not hidden.
    partial = dsse.verify_envelope(env, [trusted[0]])
    assert [r.verified for r in partial.results] == [True, False]
    assert partial.any_verified is True


def test_a_key_shipped_in_the_bundle_is_not_a_trust_root(signed_bundle) -> None:
    """The bundle carries the publisher key for convenience. It must not be believed."""
    bundle, _, _ = signed_bundle
    shipped = bytes.fromhex(bundle.member("keys/publisher_ed25519.pub").decode().strip())
    result = verify.verify_bundle(bundle, [])
    assert result.exit_code == verify.CHECK_EXIT_CODES[2]
    # ... and it IS the right key, so the failure above is about trust and not about bytes.
    assert verify.verify_bundle(
        bundle, [dsse.TrustedKey(shipped, "shipped")]
    ).verified is True


# =====================================================================================
# 2. The happy path. The 0.2.0 gate's `report-offline-verify`.
# =====================================================================================
def test_bundle_verifies_and_prints_seven_checks(signed_bundle, trusted) -> None:
    bundle, report, _ = signed_bundle
    result = verify.verify_bundle(bundle, trusted)
    assert result.exit_code == 0, verify.render(result)
    assert [c.number for c in result.checks] == [1, 2, 3, 4, 5, 6, 7]
    assert all(c.ok for c in result.checks)
    rendered = verify.render(result)
    for i in range(1, 8):
        assert f"[{i}/7]" in rendered
    assert f"VERDICT {report['verdict']}" in rendered
    assert report["verdict"] == "PASS"


def test_advisory_criterion_fails_and_the_verdict_is_still_pass(signed_bundle) -> None:
    """MOS-EVID-084: advisory criteria are reported and never affect the verdict."""
    _, report, _ = signed_bundle
    by_id = {r["id"]: r for r in report["criteria_results"]}
    assert by_id["dice_thick_slice"]["severity"] == "advisory"
    assert by_id["dice_thick_slice"]["status"] == "FAIL"
    assert report["verdict"] == "PASS"


def test_regression_criterion_is_skipped_not_passed(signed_bundle) -> None:
    """MOS-EVID-085/086: a paired test needs the incumbent run, which is not in a bundle."""
    _, report, _ = signed_bundle
    by_id = {r["id"]: r for r in report["criteria_results"]}
    assert by_id["ni_dice_positive"]["status"] == "SKIPPED"
    assert by_id["ni_dice_positive"]["reason"] == "no_incumbent_first_deployment"


def test_the_verifier_and_the_gate_agree_on_the_criteria_grammar(signed_bundle) -> None:
    """Two implementations of section 7.8.1 exist, and this is where they are compared.

    §15.2.5 requires "One evaluation implementation, not two." The verifier deliberately
    RECOMPUTES rather than calling the runner -- a check that asks the producer to confirm
    itself catches tampering and not defects -- so a second evaluator is the price of check
    5 meaning anything. What is NOT acceptable is two evaluators that never meet: that is
    how a report becomes unverifiable on a machine whose code is one release older.

    So the selector grammar and the absolute-criterion evaluator are compared here against
    `medos.evidence.criteria`, which the deployment gate uses. A divergence is a defect in
    whichever one is wrong, and it fails this test rather than a site's acceptance run.

    REPORTED as still owed: the same reconciliation for `medos.evidence.aggregate`, which
    landed after this file was written.
    """
    from medos.evidence import criteria as gate_criteria  # noqa: PLC0415

    strata_cases = [
        ({"reference_volume_ml": 12.0, "slice_thickness_mm": 3.5}, {}),
        ({"reference_volume_ml": 12.0}, {"reference_volume_ml": {"op": ">=", "value": 10.0}}),
        ({"reference_volume_ml": 0.0}, {"reference_volume_ml": {"op": "<", "value": 10.0}}),
        ({"manufacturer": "SIEMENS"},
         {"manufacturer": {"op": "in", "value": ["GE MEDICAL SYSTEMS"]}}),
        ({"manufacturer": "SIEMENS"}, {"manufacturer": {"op": "not_in", "value": ["GE"]}}),
        # The one that matters: a field the run never persisted MUST NOT match, or the
        # stratum silently widens to the whole cohort (MOS-EVID-067).
        ({"reference_volume_ml": 12.0}, {"scanner_serial": {"op": "==", "value": "x"}}),
        ({"kernel": None}, {"kernel": {"op": "==", "value": None}}),
    ]
    for strata, selector in strata_cases:
        assert verify._selector_matches(strata, selector) == gate_criteria.selector_matches(
            strata, selector
        ), (strata, selector)

    _, report, criteria = signed_bundle
    mine = {
        (a["metric"], a["stratum"]): (
            a["value"], a["ci_low"], a["ci_high"], a["n"], a["n_patients"]
        )
        for a in report["aggregates"]
    }
    theirs = {(a["metric"], a["stratum"]): a for a in report["aggregates"]}
    thresholds = report["evaluation_run"]["operating_thresholds"]
    for criterion in criteria["spec"]["absolute"]:
        status, _ = verify._evaluate_absolute(criterion, mine, thresholds)
        gate = gate_criteria.evaluate_absolute(
            criterion, theirs, operating_thresholds=thresholds
        )
        assert status == gate.status, criterion["id"]


def test_aggregates_are_independently_correct(signed_bundle) -> None:
    """One figure, recomputed by hand from the fixture definition rather than by the code.

    This is the test that stops check 5 from being a tautology in the happy path: the
    fixture takes its aggregate values from the verifier, so only an independent
    computation shows the verifier is right about anything.
    """
    _, report, _ = signed_bundle
    expected = [
        2.0 * c["intersection_voxels"] / (c["gt_voxels"] + c["pred_voxels"])
        for c in _cases()
        if c["gt_voxels"] > 0
    ]
    by_hand = sum(expected) / len(expected)
    agg = next(
        a for a in report["aggregates"]
        if a["metric"] == "dice_mean_per_case" and a["stratum"] == "gt_positive"
    )
    assert agg["n"] == len(expected)
    assert abs(agg["value"] - by_hand) < 1e-12
    # MOS-EVID-051: the empty-ground-truth cases are NOT in the Dice aggregate, and they
    # are counted in the empty-GT block instead.
    count = next(a for a in report["aggregates"] if a["metric"] == "empty_gt_case_count")
    assert count["value"] == sum(1 for c in _cases() if c["gt_voxels"] == 0)
    assert agg["n"] + count["value"] == len(_cases())


def test_bundle_is_deterministic(publisher_key) -> None:
    """A content-addressed artifact whose digest depends on the clock is not one.

    `validation_reports.bundle_digest` is a column and `MOS-EVID-123` check 1 digests every
    member, so the tar and gzip headers must not carry a timestamp. Two packs of the same
    members must be byte-identical.
    """
    bundle, _, _ = _make_bundle(publisher_key)
    again = build_bundle(bundle.report_id, bundle.members)
    assert again.archive == bundle.archive
    assert again.digest == bundle.digest


# =====================================================================================
# 3. Tampering. Chapter 7 acceptance check 34, one test per named exit code.
# =====================================================================================
def test_one_flipped_byte_in_case_metrics_exits_2(signed_bundle, trusted) -> None:
    """"a bundle with one byte flipped in `case_metrics.csv` exits 2"."""
    bundle, _, _ = signed_bundle
    members = dict(bundle.members)
    data = bytearray(members["case_metrics.csv"])
    data[len(data) // 2] ^= 0x01
    members["case_metrics.csv"] = bytes(data)
    result = verify.verify_bundle(_repack(bundle, members), trusted)
    assert result.exit_code == 2
    assert result.checks[0].number == 1 and not result.checks[0].ok


def test_report_edited_without_resigning_exits_3(signed_bundle, trusted) -> None:
    """The DSSE payload no longer decodes to `report.json`. MOS-EVID-123 check 2.

    The attacker here updated `CHECKSUMS.sha256` -- otherwise check 1 would catch it and
    the interesting check would never run.
    """
    bundle, report, _ = signed_bundle
    edited = dict(report)
    edited["verdict"] = "PASS"
    edited["engineering"] = {**report["engineering"], "p95_latency_ms": 1}
    members = dict(bundle.members)
    members["report.json"] = canonical_bytes(edited)
    members["CHECKSUMS.sha256"] = checksums_text(members)
    result = verify.verify_bundle(_repack(bundle, members), trusted)
    assert result.exit_code == 3


def test_resigned_with_an_untrusted_key_exits_4(signed_bundle, trusted) -> None:
    """"a re-signed-but-tampered `report.json` exits 3 or 4" -- this is the 4 case.

    A forger who holds a key the site does not trust can produce an internally consistent
    bundle. That is exactly what the trust bundle is for, and it is why the key shipped
    inside the archive is never consulted.
    """
    bundle, report, _ = signed_bundle
    forger = dsse.generate_keypair()
    edited = {**report, "engineering": {**report["engineering"], "p95_latency_ms": 1}}
    payload, envelope_bytes = _resign(edited, forger)
    members = dict(bundle.members)
    members["report.json"] = payload
    members["report.dsse.json"] = envelope_bytes
    members["CHECKSUMS.sha256"] = checksums_text(members)
    result = verify.verify_bundle(_repack(bundle, members), trusted)
    assert result.exit_code == 4
    assert all(not s["verified"] for s in result.signatures)


def test_schema_violation_exits_5(signed_bundle, trusted, publisher_key) -> None:
    """MOS-EVID-049: a key named `dice` MUST fail schema validation.

    Re-signed with the REAL key, so checks 1-3 pass: the point is that a valid signature
    over an invalid document is still an invalid document.
    """
    bundle, report, _ = signed_bundle
    edited = {**report, "engineering": {**report["engineering"], "dice": 0.91}}
    payload, envelope_bytes = _resign(edited, publisher_key)
    members = dict(bundle.members)
    members["report.json"] = payload
    members["report.dsse.json"] = envelope_bytes
    members["CHECKSUMS.sha256"] = checksums_text(members)
    result = verify.verify_bundle(_repack(bundle, members), trusted)
    assert result.exit_code == 5
    assert "MOS-EVID-049" in result.checks[3].detail


def test_doctored_aggregate_exits_6(signed_bundle, trusted, publisher_key) -> None:
    """"a doctored aggregate exits 6" -- with a VALID signature over the doctored document.

    This is the test the whole component exists for. Chapter 18 observed that the platform
    signs model artifacts but not the claims about them; signing the claim is only worth
    something if a signature cannot make a false number true. Here the publisher's own key
    signs a report whose headline Dice has been raised, and verification still fails --
    because check 5 recomputes it from the per-case rows the bundle ships (MOS-EVID-066).
    """
    bundle, report, _ = signed_bundle
    edited = json.loads(json.dumps(report))
    for agg in edited["aggregates"]:
        if agg["metric"] == "dice_mean_per_case" and agg["stratum"] == "gt_positive":
            agg["value"] = round(agg["value"] + 0.08, 6)
            agg["ci_low"] = round(agg["ci_low"] + 0.08, 6)
    payload, envelope_bytes = _resign(edited, publisher_key)
    members = dict(bundle.members)
    members["report.json"] = payload
    members["report.dsse.json"] = envelope_bytes
    members["CHECKSUMS.sha256"] = checksums_text(members)
    result = verify.verify_bundle(_repack(bundle, members), trusted)
    assert result.exit_code == 6
    assert "dice_mean_per_case" in result.checks[4].detail
    # Checks 1-4 passed: the document is authentic, complete and well-formed. It is simply
    # not true, and only check 5 can say so.
    assert [c.ok for c in result.checks[:4]] == [True, True, True, True]


def test_doctored_verdict_exits_7(signed_bundle, trusted, publisher_key) -> None:
    """"a doctored `verdict` exits 7". MOS-EVID-114's distinct exit code."""
    bundle, report, _ = signed_bundle
    edited = json.loads(json.dumps(report))
    for entry in edited["criteria_results"]:
        if entry["id"] == "dice_positive":
            entry["status"] = "FAIL"
    # The verdict is left at PASS while a blocking criterion now says FAIL.
    payload, envelope_bytes = _resign(edited, publisher_key)
    members = dict(bundle.members)
    members["report.json"] = payload
    members["report.dsse.json"] = envelope_bytes
    members["CHECKSUMS.sha256"] = checksums_text(members)
    result = verify.verify_bundle(_repack(bundle, members), trusted)
    assert result.exit_code == 7


def test_a_criterion_row_that_reads_false_fails_verification(
    signed_bundle, trusted, publisher_key
) -> None:
    """The numbers BESIDE the status are checked too, not only the status.

    `criteria_results[*].observed` is what a reviewer's eye lands on. A row reading
    "observed 0.95, required 0.90, PASS" over an aggregate of 0.81 derives exactly the same
    verdict, so `MOS-EVID-114` alone does not catch it -- and it is the single most
    readable lie a report can tell. Same for `required`: a row that renames the bar it was
    measured against.
    """
    bundle, report, _ = signed_bundle
    for mutate, fragment in (
        (lambda e: e.__setitem__("observed", 0.999), "observed"),
        (lambda e: e.__setitem__("required", 0.10), "required"),
        (lambda e: e.__setitem__("bound", "point"), "bound"),
    ):
        edited = json.loads(json.dumps(report))
        for entry in edited["criteria_results"]:
            if entry["id"] == "dice_positive":
                mutate(entry)
        payload, envelope_bytes = _resign(edited, publisher_key)
        members = dict(bundle.members)
        members["report.json"] = payload
        members["report.dsse.json"] = envelope_bytes
        members["CHECKSUMS.sha256"] = checksums_text(members)
        result = verify.verify_bundle(_repack(bundle, members), trusted)
        assert result.exit_code == 7, fragment
        assert fragment in result.checks[5].detail


def test_omitted_criterion_fails_verification(signed_bundle, trusted, publisher_key) -> None:
    """MOS-EVID-113: "A report that omits a criterion MUST fail verification."

    The omitted one is the advisory criterion that FAILED. Dropping it changes nothing
    about the verdict, which is precisely why a report would be tempted to drop it.
    """
    bundle, report, _ = signed_bundle
    edited = json.loads(json.dumps(report))
    edited["criteria_results"] = [
        r for r in edited["criteria_results"] if r["id"] != "dice_thick_slice"
    ]
    payload, envelope_bytes = _resign(edited, publisher_key)
    members = dict(bundle.members)
    members["report.json"] = payload
    members["report.dsse.json"] = envelope_bytes
    members["CHECKSUMS.sha256"] = checksums_text(members)
    result = verify.verify_bundle(_repack(bundle, members), trusted)
    assert result.exit_code == 7
    assert "dice_thick_slice" in result.checks[5].detail


def test_swapped_criteria_document_exits_8(signed_bundle, trusted) -> None:
    """A bundle carrying an easier criteria document than the report was measured against.

    Check 7 catches it: `capability.criteria_digest` in the signed report no longer matches
    the bundled `criteria.yaml`. Without this check, an attacker with write access to the
    archive could lower every bar without touching the signed document at all.
    """
    bundle, _, criteria = signed_bundle

    # Attack 1: relabel the document as a different, older criteria version. Nothing about
    # the re-evaluation changes -- every criterion still produces the same status -- so
    # checks 5 and 6 pass and only the digest binding can notice.
    relabelled = json.loads(json.dumps(criteria))
    relabelled["metadata"]["version"] = 2
    relabelled["metadata"]["authored_by"] = "someone-else@example"
    members = dict(bundle.members)
    members["criteria.yaml"] = canonical_bytes(relabelled)
    members["CHECKSUMS.sha256"] = checksums_text(members)
    result = verify.verify_bundle(_repack(bundle, members), trusted)
    assert result.exit_code == 8
    assert "criteria.yaml" in result.checks[6].detail
    assert [c.ok for c in result.checks[:6]] == [True] * 6

    # Attack 2: lower every bar. That one never reaches check 7, because re-evaluating the
    # criteria against the recomputed aggregates already disagrees with the stated results.
    easier = json.loads(json.dumps(criteria))
    for criterion in easier["spec"]["absolute"]:
        if "value" in criterion:
            criterion["value"] = 0.0
    members = dict(bundle.members)
    members["criteria.yaml"] = canonical_bytes(easier)
    members["CHECKSUMS.sha256"] = checksums_text(members)
    assert verify.verify_bundle(_repack(bundle, members), trusted).exit_code == 7


def test_an_extra_unlisted_member_is_refused(signed_bundle, trusted) -> None:
    """CHECKSUMS.sha256 is checked in BOTH directions -- see `_check_1_checksums`."""
    bundle, _, _ = signed_bundle
    members = dict(bundle.members)
    members["report.json.orig"] = b"{}"
    result = verify.verify_bundle(_repack(bundle, members), trusted)
    assert result.exit_code == 2
    assert "unlisted" in result.checks[0].detail


def test_hostile_archives_are_refused_without_touching_the_filesystem() -> None:
    """A verifier's whole job is handling a file it does not trust.

    `read_bundle` never extracts to disk, so a traversing member name cannot write one --
    but it is still refused rather than silently renamed, because an archive that carries
    `../../etc/passwd` is not a bundle and reporting it as "checksums failed" would send
    the operator looking for the wrong thing.
    """
    import gzip  # noqa: PLC0415
    import io  # noqa: PLC0415
    import tarfile  # noqa: PLC0415

    def pack(build) -> bytes:
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            build(tar)
        return gzip.compress(raw.getvalue(), mtime=0)

    def add(tar: Any, name: str, data: bytes = b"x") -> None:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    def traversal(tar: Any) -> None:
        add(tar, "validation-report-vr_x/../../escaped.txt")

    def symlink(tar: Any) -> None:
        info = tarfile.TarInfo("validation-report-vr_x/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)

    def two_roots(tar: Any) -> None:
        add(tar, "validation-report-vr_a/report.json")
        add(tar, "validation-report-vr_b/report.json")

    for build, fragment in (
        (traversal, "escapes"),
        (symlink, "not a regular file"),
        (two_roots, "two roots"),
    ):
        with pytest.raises(BundleError) as exc:
            read_bundle(pack(build))
        assert fragment in str(exc.value)

    assert verify.verify_archive(b"not a gzip at all", []).exit_code == verify.EXIT_USAGE


# =====================================================================================
# 4. No network. The risk §15.2.5 says this block exists to retire.
# =====================================================================================
NETWORK_MODULES = (
    "socket", "ssl", "http", "http.client", "urllib.request", "urllib3",
    "requests", "httpx", "psycopg", "ftplib", "smtplib", "asyncio",
)

_NO_NETWORK_SITECUSTOMIZE = textwrap.dedent(
    '''
    """Make this interpreter incapable of reaching the network.

    Stands in for the container with no route that chapter 7 acceptance check 34
    describes, and proves MORE than one: a container shows that a connection FAILS, this
    shows that the verifier never attempts one, because an attempt would raise here and
    the traceback would be in the output.
    """
    import socket

    class NoNetwork(OSError):
        pass

    def _refuse(*args, **kwargs):
        raise NoNetwork("this machine has no route to the MedicalOS platform")

    # A SUBCLASS and not a function: `ssl` does `class SSLSocket(socket.socket)` at import
    # time, so replacing the name with a function breaks importing `ssl` rather than
    # breaking connecting -- which would make the sandbox test the wrong thing.
    class _RefusingSocket(socket.socket):
        def __init__(self, *args, **kwargs):
            _refuse()

    socket.socket = _RefusingSocket
    socket.create_connection = _refuse
    socket.getaddrinfo = _refuse
    socket.gethostbyname = _refuse
    socket.socketpair = _refuse
    '''
)


@pytest.fixture(scope="session")
def airgapped_env(tmp_path_factory) -> dict[str, str]:
    """An environment whose interpreter cannot open a socket or resolve a name."""
    sandbox = tmp_path_factory.mktemp("airgap")
    (sandbox / "sitecustomize.py").write_text(_NO_NETWORK_SITECUSTOMIZE, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = child_pythonpath(sandbox)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def test_the_airgap_is_real(airgapped_env) -> None:
    """Prove the sandbox before trusting anything it reports.

    A no-network fixture that silently does nothing would make every test below pass while
    proving the opposite of what it claims. So: resolving the platform's own API host MUST
    fail in this interpreter.
    """
    proc = subprocess.run(
        [sys.executable, "-c",
         "import socket; socket.getaddrinfo('api.medicalos.example', 443)"],
        env=airgapped_env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0
    assert "no route to the MedicalOS platform" in proc.stderr


def test_verifier_runs_offline_and_exits_zero(
    signed_bundle, trust_bundle_pem, airgapped_env, tmp_path
) -> None:
    """The integration-suite twin of the 0.2.0 `report-offline-verify` gate check.

    THE GATE CHECK ITSELF IS `tests/gate/test_report_offline_verify.py`, and it is marked
    `gate_0_2_0`. This one is NOT marked, deliberately: `tests/gate/conftest.py` resolves a
    marked item to a check name by its MODULE, so a marked test outside `tests/gate/` would
    be selected by `pytest -m gate_0_2_0` and counted by neither the completeness guard nor
    the per-check report. `tests/unit/test_gate_contract.py` asserts that no gate marker
    appears outside `tests/gate/`.

    No database, no configuration, no MedicalOS service, and an interpreter that cannot
    open a socket. Exit 0 and all seven checks printed.
    """
    bundle, report, _ = signed_bundle
    archive = tmp_path / f"validation-report-{bundle.report_id}.tar.gz"
    archive.write_bytes(bundle.archive)
    pem = tmp_path / "trusted_publishers.pem"
    pem.write_text(trust_bundle_pem, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "medos" / "tools" / "medicalos_verify.py"),
         "report", str(archive), "--trust-bundle", str(pem)],
        env=airgapped_env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for i in range(1, 8):
        assert f"[{i}/7]" in proc.stdout
    assert f"VERDICT {report['verdict']}" in proc.stdout
    # MOS-EVID-125, on every run.
    assert "revocation status NOT checked (offline)" in proc.stdout


def test_verifier_import_closure_contains_no_network_module(tmp_path, signed_bundle,
                                                            trust_bundle_pem) -> None:
    """MOS-EVID-124 as a property of the code, not a promise about it.

    Nothing the default path imports can open a connection. A module that is imported but
    never called is still a module a later edit can call, so the assertion is over the
    import closure and not over observed traffic.

    Deliberately NOT run under `airgapped_env`: that fixture's `sitecustomize` imports
    `socket` in order to poison it, which would put `socket` in `sys.modules` before the
    verifier does anything and make this assertion fail on the sandbox rather than on the
    code. The two tests check different things and need different interpreters.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = child_pythonpath()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    bundle, _, _ = signed_bundle
    archive = tmp_path / "b.tar.gz"
    archive.write_bytes(bundle.archive)
    pem = tmp_path / "t.pem"
    pem.write_text(trust_bundle_pem, encoding="utf-8")
    script = textwrap.dedent(
        f"""
        import json, sys
        sys.argv = ["medicalos-verify", "report", {str(archive)!r},
                    "--trust-bundle", {str(pem)!r}, "--json"]
        import runpy
        try:
            runpy.run_path({str(REPO_ROOT / "medos" / "tools" / "medicalos_verify.py")!r},
                           run_name="__main__")
        except SystemExit as exc:
            code = exc.code
        loaded = sorted(m for m in {NETWORK_MODULES!r} if m in sys.modules)
        sys.stderr.write("LOADED=" + json.dumps(loaded) + " CODE=" + str(code))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert "CODE=0" in proc.stderr, proc.stdout + proc.stderr
    loaded = json.loads(proc.stderr.split("LOADED=")[1].split(" CODE=")[0])
    assert loaded == [], f"the verifier imported networking modules: {loaded}"


_NO_CRYPTOGRAPHY_SITECUSTOMIZE = textwrap.dedent(
    '''
    """A bare CPython: `cryptography` is not installed on this machine."""
    import sys

    class _Blocker:
        def find_module(self, name, path=None):
            return None

        def find_spec(self, name, path=None, target=None):
            if name == "cryptography" or name.startswith("cryptography."):
                raise ImportError("no module named 'cryptography' on this machine")
            return None

    sys.meta_path.insert(0, _Blocker())
    '''
)


def test_verifier_works_without_the_cryptography_wheel(
    signed_bundle, trust_bundle_pem, tmp_path
) -> None:
    """The verify-only RFC 8032 fallback, exercised end to end.

    The fallback exists because `MOS-EVID-122` says a bundle needs "no MedicalOS instance
    to verify", and the machine that most needs to verify one -- a hospital's acceptance
    workstation, air-gapped -- is also the machine where `pip install` is a change-control
    ticket. A fallback that is never run is a fallback that does not work, so it is run
    here against a real signed bundle with the library made unimportable.
    """
    bundle, report, _ = signed_bundle
    sandbox = tmp_path / "bare"
    sandbox.mkdir()
    (sandbox / "sitecustomize.py").write_text(
        _NO_CRYPTOGRAPHY_SITECUSTOMIZE, encoding="utf-8"
    )
    archive = tmp_path / "b.tar.gz"
    archive.write_bytes(bundle.archive)
    pem = tmp_path / "t.pem"
    pem.write_text(trust_bundle_pem, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = child_pythonpath(sandbox)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "medos" / "tools" / "medicalos_verify.py"),
         "report", str(archive), "--trust-bundle", str(pem), "--json"],
        env=env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["signature_backend"] == "rfc8032-reference"
    assert payload["verified"] is True
    assert payload["verdict"] == report["verdict"]
    assert [s["verified"] for s in payload["signatures"]] == [True]

    # ... and the same interpreter REFUSES to sign, rather than falling back to
    # variable-time Python for a private-key operation (MOS-EVID-119).
    signing = subprocess.run(
        [sys.executable, "-c",
         "from medos.evidence import dsse; dsse.sign(b'0'*32, b'x')"],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert signing.returncode != 0
    assert "SignatureBackendMissing" in signing.stderr


def test_check_revocation_is_the_only_networked_path(
    signed_bundle, trust_bundle_pem, airgapped_env, tmp_path
) -> None:
    """MOS-EVID-124 and MOS-EVID-125 together.

    With the flag, the verifier tries to reach the feed; the air-gapped interpreter refuses
    it; and the answer is `unknown`, never `not revoked`. The seven checks are unaffected,
    because `MOS-EVID-123`'s exit-code table is exhaustive and revocation is not one of
    them -- the gate reads the field, which is where INDETERMINATE is produced.
    """
    bundle, _, _ = signed_bundle
    archive = tmp_path / "b.tar.gz"
    archive.write_bytes(bundle.archive)
    pem = tmp_path / "t.pem"
    pem.write_text(trust_bundle_pem, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "medos" / "tools" / "medicalos_verify.py"),
         "report", str(archive), "--trust-bundle", str(pem), "--json",
         "--check-revocation", "https://revocation.medicalos.example/feed.json"],
        env=airgapped_env, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["verified"] is True
    assert payload["revocation"].startswith("unknown")
    assert "INDETERMINATE" in payload["revocation"]
    assert "not revoked" not in payload["revocation"]


# =====================================================================================
# 5. PHI. MOS-EVID-011, MOS-EVID-116, chapter 7 acceptance check 36.
# =====================================================================================
def test_bundle_carries_no_source_identifier(signed_bundle) -> None:
    """Scan every text member for a source UID, a patient identifier or an 8-digit date."""
    bundle, report, _ = signed_bundle
    forbidden = (
        SOURCE_STUDY_UID, SOURCE_SERIES_UID, SOURCE_PATIENT_ID,
        SOURCE_PATIENT_NAME, SOURCE_ACCESSION, SOURCE_BIRTH_DATE,
    )
    for path, data in bundle.members.items():
        if path.startswith("figures/"):
            continue
        text = data.decode("utf-8", errors="replace")
        for value in forbidden:
            assert value not in text, f"{path} leaks a source identifier"
    # And the re-keying actually happened: no tenant-side patient key survives.
    joined = b"".join(bundle.members.values()).decode("utf-8", errors="replace")
    assert "pk_tenantside" not in joined
    assert report["patient_key_scheme"]["report_salt_id"].startswith("rsalt_")


def test_rekeying_preserves_joins_and_destroys_linkage(publisher_key) -> None:
    """MOS-EVID-011: "internally joinable but not linkable back to the originating tenant".

    Two exports of the SAME cohort must produce different patient keys -- otherwise the
    per-report salt is decoration and two bundles can be joined against each other.
    """
    rows = _case_metric_rows()
    salt_a, _ = new_export_salt()
    salt_b, _ = new_export_salt()
    out_a, map_a, case_a = rekeyed_case_rows(rows, report_salt=salt_a)
    out_b, map_b, _ = rekeyed_case_rows(rows, report_salt=salt_b)
    assert set(map_a.values()).isdisjoint(map_b.values())
    # Within one export, the two studies of patient 0 still share a key: the cluster
    # bootstrap (MOS-EVID-057) depends on exactly that.
    keys_for_patient_0 = {
        row[1] for row in out_a if row[0] in {case_a[r["case_key"]] for r in rows
                                              if r["patient_key"] == "pk_tenantside0000"}
    }
    assert len(keys_for_patient_0) == 1
    assert len(out_a) == len(out_b)


def test_export_refuses_when_a_source_value_would_leak(publisher_key) -> None:
    """The PHI scan refuses the export; it does not warn about a file already written."""
    with pytest.raises(ReportRefused) as exc:
        _leaky_export(publisher_key)
    assert "MOS-EVID-116" in exc.value.check_ids
    # The refusal never echoes the leaked value -- a leak detector that prints the leak is
    # a second leak.
    assert SOURCE_PATIENT_ID not in str(exc.value)


def _leaky_export(publisher_key) -> None:
    bundle, report, criteria = _make_bundle(publisher_key)
    leaked = json.loads(json.dumps(criteria))
    leaked["metadata"]["authored_by"] = f"{SOURCE_PATIENT_NAME}@hospital.example"
    export_bundle(
        report=report,
        signers=[publisher_key],
        principal_scope=APPROVER_SCOPE,
        criteria_doc=leaked,
        applicability_envelope_doc=_envelope_doc(),
        plausibility_doc=_plausibility_doc(),
        case_metric_rows=[],
        case_score_rows=[],
        dataset_manifest=b"",
        split_manifest=b"",
        annotation_manifest=b"",
        leakage_report={},
        forbidden_source_values=[SOURCE_PATIENT_NAME, SOURCE_PATIENT_ID],
    )
    del bundle


def test_eight_digit_date_is_caught_and_a_digest_is_not() -> None:
    """The date scanner must not cry wolf on the 64-hex digests every report is full of."""
    from medos.evidence.report import phi_hits  # noqa: PLC0415

    assert phi_hits("study performed 20240117 at", forbidden=[])
    assert not phi_hits("sha256:" + "3f8b1d60c4a9e27f5b03d81a6c2e47f9" * 2, forbidden=[])
    assert not phi_hits('"issued_at":"2026-05-12T09:14:07Z"', forbidden=[])


# =====================================================================================
# 6. Signing is a separate act. MOS-EVID-119, MOS-SEC-042, acceptance check 38.
# =====================================================================================
def test_the_evaluation_runner_cannot_sign(publisher_key) -> None:
    """"attempt to sign a report using the runner's identity and expect an authorisation
    failure".

    The runner holds `validation_report.create` and `.export`. Chapter 8 gives signing its
    own permission and `MOS-SEC-042` forbids a seeded role holding both, so producing a run
    and attesting to it are different acts by different principals.
    """
    from medos.evidence.report import sign_report  # noqa: PLC0415

    doc = {"report_id": "vr_x"}
    with pytest.raises(SigningRefused) as exc:
        sign_report(doc, [publisher_key], principal_scope=list(RUNNER_SCOPE))
    assert "validation_report.approve" in str(exc.value)
    # The approver can.
    payload, envelope = sign_report(doc, [publisher_key], principal_scope=list(APPROVER_SCOPE))
    assert envelope["signatures"]


def test_an_unsigned_report_is_not_issued(publisher_key) -> None:
    """MOS-EVID-121: "an unsigned report MUST NOT be accepted by the gate"."""
    from medos.evidence.report import sign_report  # noqa: PLC0415

    with pytest.raises(SigningRefused):
        sign_report({"report_id": "vr_x"}, [], principal_scope=list(APPROVER_SCOPE))


def test_an_automated_approver_is_refused(publisher_key, trusted) -> None:
    """MOS-EVID-117: "An automated approver MUST NOT be permitted."

    The only machine-readable signal the platform has is `identity_assurance`: a service
    account's assurance names a client, not a subject.
    """
    bundle, report, _ = _make_bundle(publisher_key)
    robot = json.loads(json.dumps(report))
    robot["approver"]["identity_assurance"] = "service_account:evidence-runner"
    refusals = _issue_refusal_ids(robot, bundle, trusted)
    assert "MOS-EVID-117" in refusals


def _issue_refusal_ids(report, bundle, trusted) -> set[str]:
    from medos.evidence.reports import _issue_refusals  # noqa: PLC0415

    return {
        r.check_id
        for r in _issue_refusals(
            report=report, bundle=bundle,
            evaluation_run_ids=["11111111-1111-1111-1111-111111111111"],
            trusted_keys=trusted, cited_bundle=None,
        )
    }


def test_site_acceptance_must_present_a_verifying_vendor_bundle(publisher_key, trusted) -> None:
    """MOS-EVID-128: it "MUST fail to issue if that report does not verify offline"."""
    bundle, report, _ = _make_bundle(publisher_key, kind="site_acceptance")
    assert "MOS-EVID-128" in _issue_refusal_ids(report, bundle, trusted)


def test_validity_window_over_24_months_is_refused(publisher_key) -> None:
    """MOS-EVID-115, refused at build time as well as by the database CHECK."""
    with pytest.raises(ReportRefused) as exc:
        build_report_document(
            kind="vendor_evidence", report_version=1, subject={}, capability={},
            cohort={}, evaluation_run={}, aggregates=[], criteria_results=[],
            approver={}, applicability_envelope={}, plausibility={}, engineering={},
            artifacts={}, patient_key_scheme={},
            issued_at=datetime(2026, 1, 1, tzinfo=UTC),
            valid_for=timedelta(days=800),
        )
    assert "MOS-EVID-115" in exc.value.check_ids


def test_monitoring_period_verdict_is_forced_indeterminate() -> None:
    """MOS-EVID-142: monitoring has no reference standard, so it cannot pass anything."""
    from medos.evidence.report import derive_verdict  # noqa: PLC0415

    passing = [{"id": "a", "status": "PASS", "severity": "blocking"}]
    assert derive_verdict(passing, kind="vendor_evidence") == "PASS"
    assert derive_verdict(passing, kind="monitoring_period") == "INDETERMINATE"


def test_no_forbidden_claim_appears_in_first_party_code() -> None:
    """MOS-EVID-005's repository grep, over the code this component owns.

    The MOS-EVID-002 disclaimer is the one permitted occurrence and it is a DENIAL, not a
    claim. That exclusion is named in `medos/medos/evidence/report.py` and reported upward as a
    contradiction between MOS-EVID-002 and MOS-EVID-005 rather than resolved in silence.
    """
    owned = [
        REPO_ROOT / "medos" / "medos" / "evidence" / name
        for name in ("bundle.py", "verify.py", "dsse.py", "reports.py")
    ] + [REPO_ROOT / "medos" / "tools" / "medicalos_verify.py",
         REPO_ROOT / "medos" / "medos" / "db" / "migrations" / "0009_validation_reports.up.sql"]
    for path in owned:
        lowered = path.read_text(encoding="utf-8").replace(DISCLAIMER, " ").lower()
        for claim in FORBIDDEN_CLAIMS:
            assert claim not in lowered, f"{path.name} contains {claim!r}"

    # `report.py` DECLARES the prohibited vocabulary, so the strings necessarily appear in
    # it -- the same position the specification is in, and the same exclusion MOS-EVID-005
    # grants itself. The exclusion is bounded rather than blanket: everything after the
    # declarations block is scanned normally, so a forbidden claim that reached the code or
    # a docstring below it still fails.
    source = (REPO_ROOT / "medos" / "medos" / "evidence" / "report.py").read_text(
        encoding="utf-8"
    )
    declarations, _, code = source.partition("_ULID_RE")
    assert "FORBIDDEN_CLAIMS" in declarations
    for claim in FORBIDDEN_CLAIMS:
        assert claim not in code.replace(DISCLAIMER, " ").lower()
    # And no GENERATED artifact carries one.
    assert forbidden_claim_hits({"disclaimer": DISCLAIMER}) == []
    assert forbidden_claim_hits({"marketing": "clinically validated"}) == [
        "clinically validated"
    ]


# =====================================================================================
# 7. The database. Migration 0009.
# =====================================================================================
@pytest.fixture()
def reports_db(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute("TRUNCATE validation_reports CASCADE")
    conn.commit()
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        from medos.db.tenancy import reset_current_tenant  # noqa: PLC0415

        reset_current_tenant(token)
        conn.close()


def _swap_dbname(url: str, dbname: str) -> str:
    head, _, _tail = url.rpartition("/")
    return f"{head}/{dbname}"


def test_migration_0009_applies_from_empty(pg_dsn: str) -> None:
    """A brand-new database, `schema.sql` plus every migration, then the invariants.

    Not "CREATE TABLE returned no error": the previous block's defect was found exactly
    this way. The migration's own assertion block (section 6) runs here too, so a column
    that is neither sealed nor named mutable fails the apply rather than CI a week later.

    A DEDICATED throwaway database, not the session-scoped `pristine_dsn`. That fixture is
    one database per SESSION and three "from empty" tests now want one -- `test_queue.py`,
    `test_evidence_schema.py` and this one. The second to run finds a schema the first
    applied and is then testing "from whatever the previous test left", which is the
    opposite of the claim it makes. Found the hard way: this test passed alone and failed
    in a full run, which is the signature of shared fixture state and not of a defect in
    the thing under test. The session DSN is used only to reach the server, so this test
    does not become a second definition of the harness's connection string.
    """
    name = f"medos_vr_{secrets.token_hex(6)}"
    with psycopg.connect(pg_dsn, autocommit=True, row_factory=dict_row) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        _assert_0009_applies_from_empty(_swap_dbname(pg_dsn, name))
    finally:
        # DROPPED, which it was not. This test created a ~14 MB database per run and
        # never removed one: 31 `medos_vr_*` databases had accumulated on the development
        # server by the time anyone counted, and `CREATE DATABASE` against a server
        # carrying them is slow enough that two `test_api.py` tests timed out connecting
        # in a full-suite run and five more errored. The test that leaked passed every
        # time -- it is the SUITE that degrades, and it degrades somewhere else, which is
        # why nobody traced it here.
        #
        # `pg_terminate_backend` first because `DROP DATABASE` raises while any session
        # is connected, so a drop without it is a drop that silently does not happen --
        # the same non-event in a `finally` that looks like cleanup in a review. Every
        # other throwaway-database site in this suite already does both; this one did
        # neither.
        with psycopg.connect(pg_dsn, autocommit=True, row_factory=dict_row) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _assert_0009_applies_from_empty(dsn: str) -> None:
    """The body of the test above, lifted so the drop can wrap it in a `finally`."""
    from medos.db.migrate import apply_migrations  # noqa: PLC0415

    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM pg_tables WHERE schemaname = 'public'"
        ).fetchone()["n"] == 0, "the fixture database is not empty"
        apply_schema(conn)
        apply_migrations(conn)
        versions = {m.version for m in discover()}
        assert "0009_validation_reports" in versions
        applied = {
            r["version"]
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        assert "0009_validation_reports" in applied

        row = conn.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE oid = 'validation_reports'::regclass"
        ).fetchone()
        assert row["relrowsecurity"] and row["relforcerowsecurity"]

        # MOS-STORE-228: no table-level UPDATE, no DELETE for the application role.
        assert not conn.execute(
            "SELECT has_table_privilege('medicalos_app','validation_reports','DELETE') AS d"
        ).fetchone()["d"]
        updatable = {
            r["column_name"]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.column_privileges "
                "WHERE grantee='medicalos_app' AND table_name='validation_reports' "
                "AND privilege_type='UPDATE'"
            ).fetchall()
        }
        assert updatable == {
            "status", "superseded_by", "revocation_reason", "reproducibility_status",
            "updated_at",
        }

        # The forward migration is REPEATABLE, not merely applicable once. The `.down.sql`
        # exists for exactly this and for nothing else: `medos.db.migrate` never runs one,
        # so if it is never exercised here it rots into a file nobody has executed.
        up = next(m for m in discover() if m.version == "0009_validation_reports")
        conn.execute(up.path.with_suffix("").with_suffix(".down.sql").read_text("utf-8"))
        assert conn.execute(
            "SELECT to_regclass('validation_reports') AS t"
        ).fetchone()["t"] is None
        conn.execute(up.sql)
        assert conn.execute(
            "SELECT to_regclass('validation_reports') AS t"
        ).fetchone()["t"] is not None


def _insert_report(conn: psycopg.Connection[Any], **overrides: Any) -> str:
    from medos.sdk.canonical import new_ulid  # noqa: PLC0415

    values: dict[str, Any] = {
        "public_id": new_ulid("vr"),
        "kind": "vendor_evidence",
        "schema_version": "medicalos.io/validation-report/v1",
        "report_version": 1,
        "subject_kind": "service_version",
        "subject_id": "pulmoai.effusion",
        "subject_version": "2.1.0",
        "capability_id": "pleural_effusion",
        "criteria_version": 3,
        "evaluation_run_ids": ["22222222-2222-2222-2222-222222222222"],
        "verdict": "PASS",
        "report_bucket": BUCKET,
        "report_object_key": "t/x/validation-reports/vr_x/report.json",
        "report_digest": "sha256:" + "11" * 32,
        "envelope_digest": "sha256:" + "22" * 32,
        "bundle_bucket": BUCKET,
        "bundle_object_key": "t/x/validation-reports/vr_x/bundle.tar.gz",
        "bundle_digest": "sha256:" + "33" * 32,
        "signature": b"{}",
        "signer_key_id": "ed25519:" + "ab" * 8,
        "approver": json.dumps({
            "name": "Dr. Jana Novakova", "role": "head_of_clinical_affairs",
            "organisation": "PulmoAI s.r.o.",
            "statement": "I approve publication of this technical evidence.",
            "identity_assurance": "oidc:pulmoai.example/sub/8f21c4",
            "approved_at": "2026-05-12T08:52:31Z",
        }),
        "approver_name": "Dr. Jana Novakova",
        "approver_role": "head_of_clinical_affairs",
        "issued_at": "2026-05-12T09:14:07Z",
        "valid_until": "2027-05-12T09:14:07Z",
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    conn.execute(
        f"INSERT INTO validation_reports (tenant_id, {columns}) "
        f"VALUES (current_tenant_id(), {marks})",
        tuple(values.values()),
    )
    return str(values["public_id"])


def test_a_monitoring_report_cannot_claim_pass(reports_db) -> None:
    """MOS-EVID-142 as a database CHECK, not as a code review comment."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with reports_db.transaction():
            _insert_report(reports_db, kind="monitoring_period", verdict="PASS")
    _insert_report(reports_db, kind="monitoring_period", verdict="INDETERMINATE")
    reports_db.commit()


def test_validity_window_is_capped_in_the_database(reports_db) -> None:
    """MOS-EVID-115. The one place this rule gets bent is a back-dated re-issue."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with reports_db.transaction():
            _insert_report(
                reports_db,
                issued_at="2026-05-12T09:14:07Z",
                valid_until="2028-06-12T09:14:07Z",
            )


def test_a_site_acceptance_row_must_cite_a_vendor_report(reports_db) -> None:
    """MOS-EVID-128, structurally."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with reports_db.transaction():
            _insert_report(reports_db, kind="site_acceptance")
    _insert_report(
        reports_db, kind="site_acceptance", cited_report_digest="sha256:" + "ab" * 32
    )
    reports_db.commit()


def test_an_approver_without_a_statement_is_refused(reports_db) -> None:
    """MOS-EVID-117's structural half."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with reports_db.transaction():
            _insert_report(
                reports_db,
                approver=json.dumps({"name": "x", "role": "y", "organisation": "z",
                                     "statement": "too short", "approved_at": "n",
                                     "identity_assurance": "oidc:a/sub/b"}),
            )


def test_signed_columns_are_sealed_and_the_row_cannot_be_deleted(reports_db) -> None:
    """MOS-EVID-013 and MOS-EVID-126, asserted through SQL and not through the repository.

    Application-level sealing is one forgotten code path away from nothing, which is why
    the chapter puts the enforcement in the database and why this test does not call
    `revoke_report` to make its point.
    """
    public_id = _insert_report(reports_db)
    reports_db.commit()

    for column, value in (
        ("report_digest", "sha256:" + "00" * 32),
        ("verdict", "FAIL"),
        ("signature", b"forged"),
        ("approver_name", "Somebody Else"),
        ("valid_until", "2030-01-01T00:00:00Z"),
    ):
        with pytest.raises(psycopg.Error) as exc:
            with reports_db.transaction():
                reports_db.execute(
                    f"UPDATE validation_reports SET {column} = %s WHERE public_id = %s",
                    (value, public_id),
                )
        # `MOS06` is `forbid_column_change`'s SQLSTATE from schema.sql. Asserted rather
        # than accepting any error, because a typo'd column name also raises -- and would
        # make this test pass while proving nothing.
        assert exc.value.sqlstate == "MOS06", column
    with pytest.raises(psycopg.Error) as exc:
        with reports_db.transaction():
            reports_db.execute(
                "DELETE FROM validation_reports WHERE public_id = %s", (public_id,)
            )
    # `MOS05` is `forbid_evidence_mutation`'s, declared by 0006 and reused by 0009.
    assert exc.value.sqlstate == "MOS05"
    # ... and the four standing columns DO change (MOS-EVID-126).
    revoke_report(reports_db, public_id=public_id, reason="cohort marked defective")
    reports_db.commit()
    row = get_report(reports_db, public_id)
    assert row["status"] == "REVOKED"
    assert row["report_digest"] == "sha256:" + "11" * 32


def test_revoking_preserves_the_bytes_and_the_bundle_still_verifies(
    reports_db, signed_bundle, trusted, publisher_key
) -> None:
    """Chapter 7 acceptance check 39.

    "Revoke a report, then re-verify the original bundle offline. Assert it still verifies
    (exit 0) and that the platform reports it as REVOKED when online." That asymmetry is
    the point: the bundle is a historical fact and revocation is a present one, and
    conflating them would either destroy the record or hide the withdrawal.
    """
    bundle, report, _ = signed_bundle
    before = bundle.archive
    public_id = _insert_report(
        reports_db,
        public_id=report["report_id"],
        report_digest=report_digest(report),
        bundle_digest=bundle.digest,
    )
    reports_db.commit()
    revoke_report(reports_db, public_id=public_id, reason="superseded by a wider cohort")
    reports_db.commit()

    row = get_report(reports_db, public_id)
    assert row["status"] == "REVOKED"
    assert row["bundle_digest"] == bundle.digest
    assert bundle.archive == before
    assert verify.verify_bundle(read_bundle(before), trusted).exit_code == 0


def test_a_defective_cohort_revokes_every_citing_report(reports_db) -> None:
    """MOS-EVID-014 / MOS-STORE-301a: the cascade is a transaction, not a background job.

    "A defective cohort must never leave a live report standing on it." So the helper takes
    the caller's connection and does not open its own -- the caller's transaction is the one
    that must contain both halves.
    """
    run = "33333333-3333-3333-3333-333333333333"
    other = "44444444-4444-4444-4444-444444444444"
    a = _insert_report(
        reports_db, evaluation_run_ids=[run], report_digest="sha256:" + "aa" * 32
    )
    b = _insert_report(
        reports_db, evaluation_run_ids=[other],
        report_digest="sha256:" + "bb" * 32, report_version=2,
    )
    reports_db.commit()
    n = revoke_reports_citing_runs(
        reports_db, run_ids=[run], reason="dataset version marked DEFECTIVE"
    )
    reports_db.commit()
    assert n == 1
    assert get_report(reports_db, a)["status"] == "REVOKED"
    assert get_report(reports_db, b)["status"] == "ACTIVE"


def test_issue_report_writes_objects_and_the_row(
    reports_db, signed_bundle, trusted
) -> None:
    """The end-to-end issue path, against a real database and a real object store."""
    bundle, report, _ = signed_bundle
    store = InMemoryManifestStore()
    issued = issue_report(
        reports_db,
        report=report,
        dsse_envelope=json.loads(bundle.member("report.dsse.json")),
        bundle=bundle,
        store=store,
        report_bucket=BUCKET,
        bundle_bucket=BUCKET,
        evaluation_run_ids=["55555555-5555-5555-5555-555555555555"],
        acceptance_criteria_id=None,
        approver_user_id=OPERATOR,
        signer_key_ids=[s["keyid"] for s in json.loads(
            bundle.member("report.dsse.json"))["signatures"]],
        trusted_keys=trusted,
    )
    reports_db.commit()
    row = get_report(reports_db, issued.report_id)
    assert row["verdict"] == "PASS"
    assert row["report_digest"] == report_digest(report)
    assert row["bundle_digest"] == bundle.digest
    assert len(store) == 2
    # MOS-STORE-314: every tenant-scoped key begins `t/{tenant_id}/`.
    assert row["report_object_key"].startswith("t/")
    assert row["bundle_object_key"].endswith("/bundle.tar.gz")


def test_a_bundle_that_does_not_verify_is_not_issued(
    reports_db, signed_bundle, trusted
) -> None:
    """Issuing a bundle that fails its own seven checks publishes an unconfirmable claim."""
    bundle, report, _ = signed_bundle
    members = dict(bundle.members)
    members["case_scores.csv"] = b"case_key,patient_key,case_score,case_label,candidates\n"
    broken = build_bundle(bundle.report_id, members)  # CHECKSUMS now stale
    with pytest.raises(ReportRefused) as exc:
        issue_report(
            reports_db, report=report,
            dsse_envelope=json.loads(bundle.member("report.dsse.json")),
            bundle=broken, store=InMemoryManifestStore(),
            report_bucket=BUCKET, bundle_bucket=BUCKET,
            evaluation_run_ids=["55555555-5555-5555-5555-555555555555"],
            acceptance_criteria_id=None, approver_user_id=OPERATOR,
            signer_key_ids=["ed25519:" + "ab" * 8], trusted_keys=trusted,
        )
    reports_db.rollback()
    assert "MOS-EVID-123" in exc.value.check_ids
    assert exc.value.as_problem()["class"] == "clinical_rejection"


def test_schema_validation_catches_the_documented_shapes(signed_bundle) -> None:
    """A few of `validate_report`'s rules, so a regression in it is not silent."""
    _, report, _ = signed_bundle
    assert validate_report(report) == []
    for mutate, fragment in (
        (lambda d: d.__setitem__("disclaimer", "trust us"), "MOS-EVID-002"),
        (lambda d: d["evaluation_run"].__setitem__("code_dirty", True), "MOS-EVID-062"),
        (lambda d: d["cohort"].__setitem__("reference_of_record", False), "MOS-EVID-090"),
        (lambda d: d["evaluation_run"]["operating_thresholds"]
            ["effusion_probability"].__setitem__("selected_on", "test"), "check 13"),
        (lambda d: d["aggregates"][0].pop("operating_point"), "MOS-EVID-055"),
        (lambda d: d.__setitem__("report_version", 0), "report_version"),
    ):
        broken = json.loads(json.dumps(report))
        mutate(broken)
        problems = validate_report(broken)
        assert any(fragment in p for p in problems), (fragment, problems)
