# SPDX-License-Identifier: Apache-2.0
"""`report-offline-verify` -- the 0.2.0 gate check of docs/spec/15-delivery.md §15.1.2.

    `report-offline-verify` (a `ValidationReport` verifies on a machine with no network
    access to MedicalOS)

§15.2.5 names this block's risk and names this check as the thing that retires it:
"whether evidence is portable rather than site-regenerated -- proved by verifying a
`ValidationReport` on a machine with no network access to the platform. If evidence must be
regenerated per site it is a cost centre; if it is portable and re-verifiable it is the
thing both hospitals and model vendors route through."

WHY A SUBPROCESS WITH A POISONED `socket` AND NOT A CONTAINER WITH NO NETWORK
-----------------------------------------------------------------------------
A network-less container would prove the weaker claim: that the verifier SUCCEEDS without a
network. `MOS-EVID-124` asks for more -- "the verifier MUST refuse to make any outbound
connection unless `--check-revocation` is passed explicitly" -- and an interpreter whose
`socket` module raises on every call, including name resolution, is what makes the
difference observable. If the verifier ever attempts a connection, this arm goes red;
in a network-less container it would silently time out or quietly fall through.

The sandbox is PROVED BEFORE IT IS TRUSTED. A no-network fixture that silently did nothing
would make this check pass while demonstrating the opposite of what it claims, so
`test_report_offline_verify_the_airgap_is_real` resolves the platform's own API host in that
same interpreter and requires it to fail.

WHAT IS VERIFIED IS A BUNDLE THIS MODULE BUILT THROUGH THE PRODUCTION PATH
--------------------------------------------------------------------------
`medos.evidence.reports.build_report_document` derives the verdict from the criteria
results (it takes no verdict argument -- `MOS-EVID-114`) and `export_bundle` signs, packs
and PHI-scans. Neither is re-implemented here. The bundle handed to the verifier is
therefore the same artifact `issue_report` would store, and the check is end to end from
"the platform produced evidence" to "a stranger's laptop believes it".

ONE TAUTOLOGY, DECLARED. The happy-path aggregates are computed with the verifier's own
recomputation, so check 5 passing on an untampered bundle proves little by itself. That is
why the TAMPER arms below exist and why they are the larger half of this module: exit 2 for
a flipped byte, exit 3 for an edited report, exit 4 for an untrusted signer.

Needs no container: a tar.gz in `tmp_path`, a PEM, and CPython.

Spec: MOS-EVID-114, MOS-EVID-116, MOS-EVID-121, MOS-EVID-123, MOS-EVID-124, MOS-EVID-125,
MOS-REL-004, MOS-REL-012.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from medos.evidence import dsse, verify
from medos.evidence.bundle import (
    CASE_METRIC_COLUMNS,
    CASE_SCORE_COLUMNS,
    Bundle,
    build_bundle,
    checksums_text,
    read_bundle,
    write_csv,
)
from medos.evidence.digest import sha256_of
from medos.evidence.dsse import public_key_hex
from medos.evidence.report import report_bytes
from medos.evidence.reports import (
    build_report_document,
    export_bundle,
    new_export_salt,
    rekeyed_case_rows,
)
from medos.sdk.canonical import canonical_bytes

from tests._support.roots import REPO_ROOT, child_pythonpath

pytestmark = pytest.mark.gate_0_2_0

#: BOTH import roots, computed in one place. `medos` is `medos/medos/` now, so a
#: child given only the repository root raises ModuleNotFoundError -- and reports it
#: as whatever this test was measuring. See `tests/_support/roots.py`.
VERIFIER = REPO_ROOT / "medos" / "tools" / "medicalos_verify.py"

#: Chapter 8 §8.3.2's approval scope. The signing principal holds it; the evaluation
#: runner does not (MOS-SEC-042).
APPROVER_SCOPE = ("validation_report.approve", "validation_report.read")

#: Tenant-side values that MUST NOT survive into an exported bundle (MOS-EVID-116). They
#: are fed to the export's own scanner, which refuses rather than warns.
SOURCE_STUDY_UID = "1.2.826.0.1.3680043.10.1.77.4001"
SOURCE_PATIENT_ID = "MRN0092231"
SOURCE_PATIENT_NAME = "NOVAKOVA^JANA"
SOURCE_ACCESSION = "ACC77120045"
SOURCE_BIRTH_DATE = "19631104"

#: §7.6's conventions, with the bootstrap replicate count lowered. B = 2000 is the
#: production default; 400 keeps this check under a second and changes no verdict here.
CONVENTIONS: dict[str, Any] = {
    "dice_aggregation": "mean_of_per_case",
    "empty_gt_policy": "exclude_and_report_separately",
    "fp_volume_threshold_ml": 10.0,
    "ci_method": "percentile_cluster_bootstrap",
    "bootstrap_b": 400,
    "bootstrap_seed": 20260101,
    "metric_registry_version": 1,
}

THRESHOLDS: dict[str, Any] = {
    "effusion_probability": {"value": 0.50, "selected_on": "tune"},
    "fp_volume_threshold_ml": {"value": 10.0, "selected_on": "tune"},
}


# ======================================================================================
# The cohort. 24 cases over 18 patients -- two patients contribute two studies each.
#
# That is not decoration. MOS-EVID-057 computes the interval over PATIENT clusters, and a
# cohort where every patient contributes exactly one case cannot tell a clustered bootstrap
# from a case-level one, so a regression to the wrong resampler would pass in silence.
# ======================================================================================
def _cases() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(14):  # reference-positive
        patient = i
        gt = 40000 + 5000 * i
        out.append(
            {
                "case": i,
                "patient": patient,
                "gt_voxels": gt,
                "pred_voxels": int(gt * (1.04 + 0.01 * (i % 5))),
                "intersection_voxels": int(gt * (0.74 + 0.012 * (i % 7))),
                "gt_volume_ml": round(gt * 0.0016, 4),
                "slice_thickness_mm": 1.25 if i % 3 else 3.5,
                "sensitivity_hit": 0.0 if i in (4, 11) else 1.0,
            }
        )
    for j, patient in enumerate((0, 1)):  # second study for two of the same patients
        gt = 62000 + 3000 * j
        out.append(
            {
                "case": 14 + j,
                "patient": patient,
                "gt_voxels": gt,
                "pred_voxels": int(gt * 1.03),
                "intersection_voxels": int(gt * 0.79),
                "gt_volume_ml": round(gt * 0.0016, 4),
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
                "slice_thickness_mm": 1.25 if k % 2 else 3.5,
                "sensitivity_hit": None,
                "specificity_hit": 1.0 if pred == 0 else 0.0,
            }
        )
    return out


def _case_metric_rows() -> list[dict[str, Any]]:
    """`evaluation_case_metrics` rows in the TENANT's own key space. §7.7.2.

    The keys carry the source study UID deliberately: the export has to re-key them, and
    `SOURCE_STUDY_UID` is then in the forbidden list the PHI scan is run against, so a
    re-keying that quietly passed the original through would refuse the export.
    """
    rows: list[dict[str, Any]] = []
    for c in _cases():
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
            "case_key": f"{SOURCE_STUDY_UID}.{c['case']}",
            "patient_key": f"pk_tenantside{c['patient']:04d}",
            "gt_voxels": c["gt_voxels"],
            "pred_voxels": c["pred_voxels"],
            "intersection_voxels": c["intersection_voxels"],
            "gt_volume_ml": c["gt_volume_ml"],
            "pred_volume_ml": round(c["pred_voxels"] * 0.0016, 4),
            "strata": strata,
        }
        empty = c["gt_voxels"] == 0
        rows.append(
            {
                **base,
                "metric": "dice_mean_per_case",
                "value": None
                if empty
                else 2.0
                * c["intersection_voxels"]
                / (c["gt_voxels"] + c["pred_voxels"]),
                # MOS-EVID-051: the empty-GT case leaves the aggregate and is reported in
                # the empty-GT block. MOS-EVID-068: its counts are persisted regardless.
                "undefined_reason": "empty_ground_truth" if empty else None,
                "eligible": not empty,
            }
        )
        for metric, key in (("sensitivity", "sensitivity_hit"),
                            ("specificity", "specificity_hit")):
            if c.get(key) is not None:
                rows.append({**base, "metric": metric, "value": c[key],
                             "undefined_reason": None, "eligible": True})
    rows.sort(key=lambda r: (r["case_key"], r["metric"]))
    return rows


def _criteria_doc() -> dict[str, Any]:
    """§7.8.2's document at the bar this 24-case cohort clears.

    The VALUES are lowered from the chapter's worked example, which is sized for n >= 300.
    The STRUCTURE is the chapter's exactly: `bound: ci_lower_95` everywhere
    (MOS-EVID-077), an empty-GT companion to the Dice criterion (MOS-EVID-052), an
    advisory criterion set above what the cohort achieves so that a PASS proves it was
    excluded from the combination rather than passed (MOS-EVID-084), and a regression
    criterion that cannot be evaluated offline and must therefore report SKIPPED.
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
                {"id": "gt_positive",
                 "selector": {"reference_volume_ml": {"op": ">=", "value": 10.0}}},
                {"id": "gt_empty",
                 "selector": {"reference_volume_ml": {"op": "<", "value": 10.0}}},
                {"id": "thick_slice",
                 "selector": {"slice_thickness_mm": {"op": ">", "value": 3.0}}},
            ],
            "absolute": [
                {"id": "sens_all", "metric": "sensitivity", "stratum": "all",
                 "operating_threshold": {"name": "effusion_probability", "value": 0.50},
                 "bound": "ci_lower_95", "op": ">=", "value": 0.60,
                 "min_cases": 12, "min_patients": 10,
                 "on_insufficient_cases": "indeterminate", "severity": "blocking"},
                {"id": "dice_positive", "metric": "dice_mean_per_case",
                 "stratum": "gt_positive", "bound": "ci_lower_95", "op": ">=",
                 "value": 0.60, "min_cases": 12, "min_patients": 10,
                 "on_insufficient_cases": "indeterminate", "severity": "blocking"},
                {"id": "fp_on_negatives", "metric": "empty_gt_false_positive_rate",
                 "stratum": "gt_empty", "bound": "ci_upper_95", "op": "<=", "value": 0.90,
                 "min_cases": 6, "min_patients": 6,
                 "on_insufficient_cases": "indeterminate", "severity": "blocking"},
                {"id": "dice_thick_slice", "metric": "dice_mean_per_case",
                 "stratum": "thick_slice", "bound": "ci_lower_95", "op": ">=",
                 "value": 0.99, "min_cases": 3, "min_patients": 3,
                 "on_insufficient_cases": "indeterminate", "severity": "advisory"},
            ],
            "regression": [
                {"id": "ni_dice_positive", "metric": "dice_mean_per_case",
                 "stratum": "gt_positive", "margin": 0.02, "bound": "ci_lower_95",
                 "min_cases": 12, "min_patients": 10,
                 "on_insufficient_cases": "indeterminate", "severity": "blocking"},
            ],
        },
    }


def _envelope_doc() -> dict[str, Any]:
    return {
        "apiVersion": "medicalos.io/v1",
        "kind": "ApplicabilityEnvelope",
        "metadata": {"subject_kind": "service_version", "subject_id": "pulmoai.effusion",
                     "subject_version": "2.1.0", "version": 4,
                     "derivation": "percentile_1_99"},
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
            ]
        },
    }


def _manifest_lines(case_map: dict[str, str], patient_of_case: dict[str, str]) -> bytes:
    """A pseudonymised dataset manifest. MOS-EVID-009's JSONL, MOS-EVID-116's keys.

    `sop_class_uid` stays: it is a DICOM constant identifying the object TYPE and says
    nothing about a patient. That is exactly why the PHI scan matches a list of known
    source values rather than a UID-shaped regex -- the regex would flag this line and be
    switched off within a week.
    """
    return b"".join(
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
        for source_case, pseudo in sorted(case_map.items(), key=lambda kv: kv[1])
    )


# ======================================================================================
# The bundle, built through the production path.
# ======================================================================================
@pytest.fixture(scope="module")
def publisher_key() -> tuple[bytes, bytes]:
    return dsse.generate_keypair()


@pytest.fixture(scope="module")
def trust_bundle_pem(publisher_key: tuple[bytes, bytes]) -> str:
    return dsse.public_key_pem(publisher_key[1], "PulmoAI s.r.o. release key")


@pytest.fixture(scope="module")
def signed_bundle(
    publisher_key: tuple[bytes, bytes],
) -> tuple[Bundle, dict[str, Any], dict[str, Any]]:
    """`(bundle, report, criteria_doc)` -- signed, packed, PHI-scanned, ready to hand over."""
    criteria, envelope, plausibility = (
        _criteria_doc(), _envelope_doc(), _plausibility_doc()
    )
    salt, salt_id = new_export_salt()
    source_rows = _case_metric_rows()
    csv_rows, patient_map, case_map = rekeyed_case_rows(source_rows, report_salt=salt)
    score_rows = [
        [case_map[k], patient_map[p], "", "", "[]"]
        for k, p in sorted({(r["case_key"], r["patient_key"]) for r in source_rows})
    ]
    case_metrics_csv = write_csv(CASE_METRIC_COLUMNS, csv_rows)
    case_scores_csv = write_csv(CASE_SCORE_COLUMNS, score_rows)
    dataset_manifest = _manifest_lines(
        case_map, {r["case_key"]: patient_map[r["patient_key"]] for r in source_rows}
    )
    split_manifest = (
        canonical_bytes({"partition": "test", "n_patients": len(patient_map)}) + b"\n"
    )
    annotation_manifest = (
        canonical_bytes({"consensus_rule": "majority_at_least_2"}) + b"\n"
    )
    leakage = {"L1": "pass", "L2": "pass", "L3": "pass", "L4": "pass", "L5": "pass",
               "waivers": []}

    # Recompute the aggregates from the EXPORTED csv, through the verifier's own path, so
    # what the report states is exactly what the per-case rows support. Declared as a
    # tautology in the module docstring and paid for by the tamper arms.
    rows = verify._load_rows(Bundle("vr_x", {"case_metrics.csv": case_metrics_csv}, b""))
    strata = {s["id"]: s["selector"] for s in criteria["spec"]["strata"]}
    wanted = [
        ("sensitivity", "all", {"name": "effusion_probability", "value": 0.50}),
        ("dice_mean_per_case", "gt_positive", None),
        ("dice_pooled", "gt_positive", None),
        ("empty_gt_false_positive_rate", "gt_empty", None),
        ("empty_gt_case_count", "gt_empty", None),
        ("dice_mean_per_case", "thick_slice", None),
    ]
    aggregates: list[dict[str, Any]] = []
    for metric, stratum, operating_point in wanted:
        subset = [r for r in rows if verify._selector_matches(r.strata, strata[stratum])]
        value, lo, hi, n, n_pat = verify._recompute(
            metric, subset, conventions=CONVENTIONS
        )
        entry: dict[str, Any] = {
            "metric": metric, "stratum": stratum, "value": value,
            "ci_low": lo, "ci_high": hi, "n": n, "n_patients": n_pat,
        }
        if operating_point is not None:
            entry["operating_point"] = operating_point
        aggregates.append(entry)

    recomputed = {
        (a["metric"], a["stratum"]): (a["value"], a["ci_low"], a["ci_high"],
                                     a["n"], a["n_patients"])
        for a in aggregates
    }
    criteria_results: list[dict[str, Any]] = []
    for criterion in criteria["spec"]["absolute"]:
        status, reason = verify._evaluate_absolute(criterion, recomputed, THRESHOLDS)
        agg = recomputed.get((criterion["metric"], criterion["stratum"]))
        entry = {
            "id": criterion["id"], "status": status,
            "severity": criterion.get("severity", "blocking"),
            "metric": criterion["metric"], "stratum": criterion["stratum"],
            "bound": criterion["bound"], "op": criterion["op"],
            "required": criterion["value"],
        }
        if agg is not None:
            entry["observed"] = {
                "point": agg[0], "ci_lower_95": agg[1], "ci_upper_95": agg[2]
            }[criterion["bound"]]
            entry["n"], entry["n_patients"] = agg[3], agg[4]
        if reason:
            entry["reason"] = reason
        criteria_results.append(entry)
    for criterion in criteria["spec"]["regression"]:
        criteria_results.append(
            {"id": criterion["id"], "status": "SKIPPED",
             "severity": criterion.get("severity", "blocking"),
             "reason": "no_incumbent_first_deployment"}
        )

    report = build_report_document(
        kind="vendor_evidence",
        report_version=1,
        subject={
            "kind": "service_version", "id": "pulmoai.effusion", "version": "2.1.0",
            "image_digest": "sha256:" + "5c" * 32,
            "legal_manufacturer": {"name": "PulmoAI s.r.o.", "id": "CZ-28471902"},
            "internal_pipeline_digest_attestation": "vendor_asserted",
        },
        capability={
            "id": "pleural_effusion", "criteria_version": 3,
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
            "operating_thresholds": THRESHOLDS,
            "metric_conventions": CONVENTIONS,
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
            "fire_rates": {"pl_volume_range": 0.0},
        },
        engineering={"p95_latency_ms": 41200, "peak_gpu_memory_mib": 9840,
                     "result_schema_valid_rate": 1.0},
        artifacts={
            "case_metrics_csv_digest": sha256_of(case_metrics_csv),
            "case_scores_csv_digest": sha256_of(case_scores_csv),
            "dataset_manifest_digest": sha256_of(dataset_manifest),
            "split_manifest_digest": sha256_of(split_manifest),
            "annotation_manifest_digest": sha256_of(annotation_manifest),
            "leakage_report_digest": sha256_of(canonical_bytes(leakage)),
        },
        patient_key_scheme={"algorithm": "hmac-sha256-b32-10", "report_salt_id": salt_id},
        issued_at=datetime(2026, 5, 12, 9, 14, 7, tzinfo=UTC),
    )
    bundle, _envelope_dsse, _payload = export_bundle(
        report=report,
        signers=[publisher_key],
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
            SOURCE_STUDY_UID, SOURCE_PATIENT_ID, SOURCE_PATIENT_NAME,
            SOURCE_ACCESSION, SOURCE_BIRTH_DATE,
        ],
    )
    return bundle, report, criteria


# ======================================================================================
# The airgap. Poisoned first, proved second, used third.
# ======================================================================================
_NO_NETWORK_SITECUSTOMIZE = textwrap.dedent(
    '''
    """An interpreter with no route to anything, MedicalOS included."""
    import socket


    def _refuse(*args, **kwargs):
        raise OSError(
            "no route to the MedicalOS platform: this interpreter has no network"
        )


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


@pytest.fixture(scope="module")
def airgapped_env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    sandbox = tmp_path_factory.mktemp("airgap")
    (sandbox / "sitecustomize.py").write_text(
        _NO_NETWORK_SITECUSTOMIZE, encoding="utf-8"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = child_pythonpath(sandbox)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


@pytest.fixture()
def handover(
    signed_bundle: tuple[Bundle, dict[str, Any], dict[str, Any]],
    trust_bundle_pem: str,
    tmp_path: Path,
) -> tuple[Path, Path]:
    """The two files a vendor hands a hospital: the archive and the trust bundle."""
    bundle, _report, _criteria = signed_bundle
    archive = tmp_path / f"validation-report-{bundle.report_id}.tar.gz"
    archive.write_bytes(bundle.archive)
    pem = tmp_path / "trusted_publishers.pem"
    pem.write_text(trust_bundle_pem, encoding="utf-8")
    return archive, pem


def _verify(
    archive: Path, pem: Path, env: dict[str, str], *extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), "report", str(archive),
         "--trust-bundle", str(pem), *extra],
        env=env, capture_output=True, text=True, timeout=300,
    )


# ======================================================================================
# 1. The sandbox is real. Asserted BEFORE anything is concluded from it.
# ======================================================================================
def test_report_offline_verify_the_airgap_is_real(airgapped_env: dict[str, str]) -> None:
    """A no-network fixture that silently did nothing would invert every arm below."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import socket; socket.getaddrinfo('api.medicalos.example', 443)"],
        env=airgapped_env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0, "the sandbox resolved the platform's API host"
    assert "no route to the MedicalOS platform" in proc.stderr


# ======================================================================================
# 2. THE CHECK. Seven checks, exit 0, no network.
# ======================================================================================
def test_report_offline_verify_a_report_verifies_with_no_route_to_the_platform(
    handover: tuple[Path, Path],
    airgapped_env: dict[str, str],
    signed_bundle: tuple[Bundle, dict[str, Any], dict[str, Any]],
) -> None:
    """§15.1.2's `report-offline-verify`, end to end through the shipped CLI.

    No database, no configuration, no MedicalOS service, and an interpreter that cannot
    open a socket or resolve a name. Every one of `MOS-EVID-123`'s seven checks is printed,
    the verdict matches the report, and `MOS-EVID-125`'s note is on the output so that a
    reader is never left to assume revocation was consulted when it was not.
    """
    archive, pem = handover
    _bundle, report, _criteria = signed_bundle
    proc = _verify(archive, pem, airgapped_env)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    for i in range(1, 8):
        assert f"[{i}/7]" in proc.stdout, f"check {i} of 7 was not reported"
    assert f"VERDICT {report['verdict']}" in proc.stdout
    assert "revocation status NOT checked (offline)" in proc.stdout


def test_report_offline_verify_attempts_no_connection_at_all(
    handover: tuple[Path, Path]
) -> None:
    """`MOS-EVID-124` as a property of the code, not a promise about it.

    Nothing the default path imports can open a connection. A module that is imported but
    never called is still a module a later edit can call, so the assertion is over the
    IMPORT CLOSURE and not over observed traffic.

    Deliberately NOT run under `airgapped_env`: that sandbox imports `socket` in order to
    poison it, which would put `socket` in `sys.modules` before the verifier does anything
    and make this assertion fail on the sandbox rather than on the code.
    """
    archive, pem = handover
    env = dict(os.environ)
    env["PYTHONPATH"] = child_pythonpath()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    network_modules = [
        "socket", "ssl", "http", "http.client", "urllib.request", "requests",
        "httpx", "asyncio", "smtplib", "ftplib", "telnetlib", "xmlrpc.client",
    ]
    script = textwrap.dedent(
        f"""
        import json, runpy, sys
        sys.argv = ["medicalos-verify", "report", {str(archive)!r},
                    "--trust-bundle", {str(pem)!r}, "--json"]
        code = None
        try:
            runpy.run_path({str(VERIFIER)!r}, run_name="__main__")
        except SystemExit as exc:
            code = exc.code
        loaded = sorted(m for m in {network_modules!r} if m in sys.modules)
        sys.stderr.write("LOADED=" + json.dumps(loaded) + " CODE=" + str(code))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True,
        timeout=300,
    )
    assert "CODE=0" in proc.stderr, proc.stdout + proc.stderr
    loaded = json.loads(proc.stderr.split("LOADED=")[1].split(" CODE=")[0])
    assert loaded == [], f"the verifier imported networking modules: {loaded}"


# ======================================================================================
# 3. The tamper arms. Without these, "it verified" means "it read a file".
# ======================================================================================
def _repack(bundle: Bundle, members: dict[str, bytes]) -> bytes:
    return build_bundle(bundle.report_id, members).archive


def test_report_offline_verify_a_flipped_byte_in_the_case_rows_exits_two(
    handover: tuple[Path, Path],
    airgapped_env: dict[str, str],
    signed_bundle: tuple[Bundle, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
) -> None:
    """`MOS-EVID-123` check 1, exit 2: the checksums no longer describe the bytes.

    A gate that reads this command reads the CODE (chapter 7 acceptance check 34), so an
    exit of 1 -- usage error -- on a doctored bundle would be indistinguishable to CI from
    a typo in the invocation. The member is edited and the archive REPACKED rather than
    corrupted in place, because a broken tar header fails for a reason that says nothing
    about evidence.
    """
    _archive, pem = handover
    bundle, _report, _criteria = signed_bundle
    members = dict(read_bundle(bundle.archive).members)
    data = bytearray(members["case_metrics.csv"])
    data[len(data) // 2] ^= 0x01
    members["case_metrics.csv"] = bytes(data)

    doctored = tmp_path / "flipped.tar.gz"
    doctored.write_bytes(_repack(bundle, members))

    proc = _verify(doctored, pem, airgapped_env)
    assert proc.returncode == 2, (
        f"one flipped byte in case_metrics.csv exited {proc.returncode}, not 2\n"
        + proc.stdout
        + proc.stderr
    )
    assert "[1/7] checksums" in proc.stdout


def test_report_offline_verify_an_edited_report_exits_three(
    handover: tuple[Path, Path],
    airgapped_env: dict[str, str],
    signed_bundle: tuple[Bundle, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
) -> None:
    """`MOS-EVID-123` check 2, exit 3: the DSSE payload no longer equals `report.json`.

    The attacker here is competent: `CHECKSUMS.sha256` is recomputed, so check 1 passes
    and the interesting check is the one that fires. What is edited is a number nobody
    would look at twice -- a latency figure, not the verdict -- because the property being
    asserted is that the signature covers the WHOLE document and not its conclusion.
    """
    _archive, pem = handover
    bundle, report, _criteria = signed_bundle
    edited = {
        **report,
        "engineering": {**report["engineering"], "p95_latency_ms": 1},
    }
    members = dict(read_bundle(bundle.archive).members)
    members["report.json"] = canonical_bytes(edited)
    members["CHECKSUMS.sha256"] = checksums_text(members)

    doctored = tmp_path / "edited.tar.gz"
    doctored.write_bytes(_repack(bundle, members))

    proc = _verify(doctored, pem, airgapped_env)
    assert proc.returncode == 3, (
        f"an edited, re-checksummed report exited {proc.returncode}, not 3\n"
        + proc.stdout
        + proc.stderr
    )


def test_report_offline_verify_a_key_inside_the_bundle_is_not_a_trust_root(
    handover: tuple[Path, Path], airgapped_env: dict[str, str],
    signed_bundle: tuple[Bundle, dict[str, Any], dict[str, Any]], tmp_path: Path,
) -> None:
    """`MOS-EVID-121`: a signature makes a document authentic, not true -- and a bundle
    that carries its own trust root makes it neither.

    The report is re-signed with a key the verifier has never been told to trust, and the
    bundle's own `publisher_key.pem` is replaced to match, so every internal consistency
    check inside the archive passes. Exit 4 -- "no presented signature verifies against the
    trust bundle" -- is the only thing standing between a hospital and an attacker's
    self-issued evidence.
    """
    _archive, pem = handover
    bundle, report, _criteria = signed_bundle
    rogue = dsse.generate_keypair()

    edited = {**report, "engineering": {**report["engineering"], "p95_latency_ms": 1}}
    payload = report_bytes(edited)
    members = dict(read_bundle(bundle.archive).members)
    members["report.json"] = payload
    members["report.dsse.json"] = canonical_bytes(dsse.build_envelope(payload, [rogue]))
    # The archive's own copy of the publisher key is swapped for the attacker's, so the
    # bundle is internally consistent from top to bottom. It still fails, because the key
    # inside a bundle is never a trust root.
    members["keys/publisher_ed25519.pub"] = public_key_hex(rogue[1]).encode("ascii")
    members["CHECKSUMS.sha256"] = checksums_text(members)

    forged = tmp_path / "forged.tar.gz"
    forged.write_bytes(_repack(bundle, members))

    proc = _verify(forged, pem, airgapped_env)
    assert proc.returncode == 4, (
        f"a bundle signed by an untrusted key exited {proc.returncode}, not 4\n"
        + proc.stdout
        + proc.stderr
    )


def test_report_offline_verify_carries_no_source_identifier(
    signed_bundle: tuple[Bundle, dict[str, Any], dict[str, Any]],
) -> None:
    """`MOS-EVID-116`: what is portable must be portable WITHOUT carrying the patients.

    The bundle is searched for the tenant-side values the cohort was built from. This is
    the one assertion in this module that would still matter if the signature scheme were
    replaced tomorrow: an exported artifact that re-identifies a cohort is not evidence
    that can leave a hospital at all, and the offline-verification property would then be
    worthless rather than valuable.
    """
    bundle, _report, _criteria = signed_bundle
    haystack = b"".join(read_bundle(bundle.archive).members.values())
    for forbidden in (SOURCE_STUDY_UID, SOURCE_PATIENT_ID, SOURCE_PATIENT_NAME,
                      SOURCE_ACCESSION, SOURCE_BIRTH_DATE):
        assert forbidden.encode("utf-8") not in haystack, (
            "a tenant-side identifier survived the export; the value is NOT reproduced "
            "here and the member list is in the bundle"
        )
