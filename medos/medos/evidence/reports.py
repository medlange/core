# SPDX-License-Identifier: Apache-2.0
"""Issuing, exporting and revoking a `ValidationReport`. Chapter 7 section 7.12.

    MOS-EVID-011   the export re-keys `patient_key` under a per-report salt.
    MOS-EVID-014   marking a DatasetVersion DEFECTIVE cascades REVOKED onto every report
                   citing its runs. The cascade is a TRANSACTION, not a background job.
    MOS-EVID-113   `criteria_results` carries one entry per criterion.
    MOS-EVID-114   `verdict` is DERIVED here, never accepted from a caller.
    MOS-EVID-115   `valid_until` is set and never exceeds 24 months.
    MOS-EVID-116   no PHI leaves in the bundle. Scanned before the archive is written.
    MOS-EVID-117   `approver` names a real human. An automated approver is refused.
    MOS-EVID-119   signing is a separately authorised act; the runner cannot perform it.
    MOS-EVID-121   an unsigned report is not issued at all.
    MOS-EVID-126   revocation preserves the bytes: `status` changes, nothing else does.
    MOS-EVID-128   a `site_acceptance` report cites a `vendor_evidence` report by digest
                   and MUST FAIL TO ISSUE if that report does not verify offline.
    MOS-STORE-343  `approver_name` and `approver_role` are denormalised so the report
                   keeps naming its approver after the account is gone.

WHERE THE REFUSALS LIVE
-----------------------
Every rule above that a database CHECK can express is ALSO a CHECK in
`0009_validation_reports.up.sql`. The duplication is deliberate and is the same
arrangement `medos.evidence.repo` uses for the seal: the constraint is the guarantee, the
refusal here is the good error message, and the two are tested against each other. The
rules that cannot be a CHECK -- "the cited vendor report verifies offline", "the approver
is not a service account" -- exist ONLY here, and that is stated at each one so a reader
knows which layer is load-bearing.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from medos.evidence.bundle import (
    Bundle,
    assemble_members,
    build_bundle,
    bundle_object_key,
    case_pseudonym,
    read_bundle,
    rekey_patient,
    report_object_key,
    report_salt_id,
)
from medos.evidence.digest import sha256_of
from medos.evidence.dsse import TrustedKey, new_report_salt
from medos.evidence.report import (
    DISCLAIMER,
    SCHEMA_VERSION,
    derive_verdict,
    new_report_id,
    report_bytes,
    report_digest,
    sign_report,
    validate_report,
)
from medos.evidence.store import ManifestStore
from medos.evidence.verify import verify_archive
from medos.sdk.refusal import Refusal, RefusalError

__all__ = [
    "ReportRefused",
    "scan_members_for_phi",
    "IssuedReport",
    "build_report_document",
    "export_bundle",
    "issue_report",
    "get_report",
    "revoke_report",
    "supersede_report",
    "mark_degraded",
    "revoke_reports_citing_runs",
]


class ReportRefused(RefusalError):
    """A `ValidationReport` MUST NOT be issued. MOS-EVID-117 / -121 / -128 / -142.

    Shares `medos.sdk.refusal`' refusal shape with `SealRefused` and
    `FreezeRefused` on purpose: a caller that already handles a refused seal handles a
    refused issue with the same code, and `as_problem()` renders the same RFC 9457 body
    with `class: clinical_rejection`.
    """

    problem_type = "https://medicalos.dev/problems/validation-report-refused"
    title = "the ValidationReport was not issued"


@dataclass(frozen=True)
class IssuedReport:
    report_id: str
    report_digest: str
    bundle_digest: str
    verdict: str
    signer_key_ids: tuple[str, ...]
    bundle: Bundle


# =====================================================================================
# 1. The document.  Section 7.12.1.
# =====================================================================================
def build_report_document(
    *,
    kind: str,
    report_version: int,
    subject: dict[str, Any],
    capability: dict[str, Any],
    cohort: dict[str, Any],
    evaluation_run: dict[str, Any],
    aggregates: Sequence[dict[str, Any]],
    criteria_results: Sequence[dict[str, Any]],
    approver: dict[str, Any],
    applicability_envelope: dict[str, Any],
    plausibility: dict[str, Any],
    engineering: dict[str, Any],
    artifacts: dict[str, Any],
    patient_key_scheme: dict[str, Any],
    issued_at: datetime,
    valid_for: timedelta = timedelta(days=365),
    regression_results: Sequence[dict[str, Any]] = (),
    cites: dict[str, Any] | None = None,
    report_id: str | None = None,
) -> dict[str, Any]:
    """Assemble `report.json`. The VERDICT IS DERIVED, never taken from the caller.

    `MOS-EVID-114` makes the verdict re-derivable from `criteria_results`, and a builder
    that accepted a verdict argument would let a caller state one that its own criteria do
    not support -- which is the exact document the verifier's check 6 exists to reject.
    Deriving it here means a report can only carry a verdict its criteria produced.

    `valid_for` defaults to twelve months rather than `MOS-EVID-115`'s twenty-four-month
    ceiling: a default at the maximum makes the maximum the norm, and expiry is a freshness
    signal whose value comes from being shorter than the artifact's life.
    """
    if valid_for > timedelta(days=731):
        raise ReportRefused([
            Refusal(
                check_id="MOS-EVID-115",
                code="validity_window_too_long",
                message="valid_until MUST NOT exceed 24 months from issued_at",
                observed=str(valid_for),
                bound="731 days",
            )
        ])
    issued = issued_at.astimezone(UTC).replace(microsecond=0)
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "report_id": report_id or new_report_id(),
        "report_version": int(report_version),
        "kind": kind,
        "issued_at": _iso(issued),
        "valid_until": _iso(issued + valid_for),
        "disclaimer": DISCLAIMER,  # MOS-EVID-002, verbatim
        "subject": dict(subject),
        "capability": dict(capability),
        "cohort": dict(cohort),
        "evaluation_run": dict(evaluation_run),
        "aggregates": [dict(a) for a in aggregates],
        "criteria_results": [dict(r) for r in criteria_results],
        "regression_results": [dict(r) for r in regression_results],
        "applicability_envelope": dict(applicability_envelope),
        "plausibility": dict(plausibility),
        "engineering": dict(engineering),
        "approver": dict(approver),
        "artifacts": dict(artifacts),
        "patient_key_scheme": dict(patient_key_scheme),
        "verdict": derive_verdict([dict(r) for r in criteria_results], kind=kind),
    }
    if cites:
        doc["cites"] = dict(cites)
    return doc


# =====================================================================================
# 2. The export.  Section 7.12.3.
# =====================================================================================
def export_bundle(
    *,
    report: dict[str, Any],
    signers: Sequence[tuple[bytes, bytes]],
    principal_scope: Sequence[str],
    criteria_doc: dict[str, Any],
    applicability_envelope_doc: dict[str, Any],
    plausibility_doc: dict[str, Any],
    case_metric_rows: Sequence[Sequence[Any]],
    case_score_rows: Sequence[Sequence[Any]],
    dataset_manifest: bytes,
    split_manifest: bytes,
    annotation_manifest: bytes,
    leakage_report: dict[str, Any],
    forbidden_source_values: Sequence[str] = (),
    figures: dict[str, bytes] | None = None,
) -> tuple[Bundle, dict[str, Any], bytes]:
    """Sign the document and pack the bundle. `(bundle, dsse_envelope, report_bytes)`.

    Order matters and is not incidental: the report is signed over the bytes that go into
    `report.json`, then the members are assembled, then `CHECKSUMS.sha256` is computed over
    the assembled members, then the archive is packed. Any other order produces a bundle
    whose checksum file describes a different set of bytes than the ones it ships with.

    The PHI scan runs on the ASSEMBLED members, after re-keying and pseudonymisation, and
    refuses the export rather than warning. `MOS-EVID-116` is a property of the exported
    artifact, and a warning on a file that has already been written is not a control.
    """
    payload, dsse_envelope = sign_report(
        report, [(s, p) for s, p in signers], principal_scope=list(principal_scope)
    )
    public_key = signers[0][1]
    members = assemble_members(
        report=report,
        dsse_envelope=dsse_envelope,
        criteria_doc=criteria_doc,
        applicability_envelope_doc=applicability_envelope_doc,
        plausibility_doc=plausibility_doc,
        case_metric_rows=case_metric_rows,
        case_score_rows=case_score_rows,
        dataset_manifest=dataset_manifest,
        split_manifest=split_manifest,
        annotation_manifest=annotation_manifest,
        leakage_report=leakage_report,
        public_key=public_key,
        figures=figures,
    )

    refusals = [
        Refusal(
            check_id="MOS-EVID-116",
            code="phi_in_bundle",
            message=f"{path}: {hit}",
            detail={"member": path},
        )
        for path, hit in scan_members_for_phi(members, list(forbidden_source_values))
    ]
    if refusals:
        raise ReportRefused(refusals)

    bundle = build_bundle(str(report["report_id"]), members)
    return bundle, dsse_envelope, payload


#: `case_metrics.csv` columns whose values are numbers. They are excluded from the
#: date-shaped-token scan for the reason `phi_hits` gives: a voxel count of 20_000_000 and
#: a birth date of 19631104 are the same eight characters, and only one of them is PHI.
_NUMERIC_CSV_COLUMNS: frozenset[str] = frozenset(
    {
        "value", "gt_voxels", "pred_voxels", "intersection_voxels",
        "gt_volume_ml", "pred_volume_ml", "case_score", "case_label",
    }
)


def scan_members_for_phi(
    members: dict[str, bytes], forbidden: Sequence[str]
) -> list[tuple[str, str]]:
    """`[(member, hit)]` for MOS-EVID-116, dispatching on what each member IS.

    Chapter 7 acceptance check 36 asks for three scans over every exported bundle: source
    UIDs, eight-digit dates, and any string from the source `PatientName`/`PatientID`. Done
    naively over raw bytes, the second one fires on `"bootstrap_seed": 20260101` -- a number
    the specification's own worked example prints -- and on any voxel count above ten
    million. This dispatch keeps the check able to fire only where a DICOM string value can
    actually be, which is what keeps it switched on.
    """
    from medos.evidence.bundle import CASE_METRIC_COLUMNS, CASE_SCORE_COLUMNS, read_csv
    from medos.evidence.report import phi_hits, phi_hits_in_json  # noqa: PLC0415

    wanted = list(forbidden)
    out: list[tuple[str, str]] = []
    for path, data in sorted(members.items()):
        # `report.dsse.json` is `report.json` base64-encoded, and `report.json` is scanned
        # below. Scanning the encoded copy finds nothing the plain one does not and DOES
        # introduce a false positive: base64 uses `+`, `/` and `=`, so an eight-digit run
        # inside the payload can end up bounded by non-alphanumerics and read as a date.
        # A scanner that occasionally refuses a clean export is a scanner that gets
        # switched off.
        if (
            path.startswith("figures/")
            or path == "keys/publisher_ed25519.pub"
            or path == "report.dsse.json"
        ):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            out.append((path, "a bundle member is not UTF-8 text and was not scanned"))
            continue
        if path.endswith((".json", ".yaml")):
            out.extend(
                (path, hit)
                for hit in phi_hits_in_json(json.loads(text), forbidden=wanted)
            )
        elif path.endswith(".jsonl"):
            for i, line in enumerate(text.splitlines()):
                if line.strip():
                    out.extend(
                        (path, f"line {i + 1} {hit}")
                        for hit in phi_hits_in_json(json.loads(line), forbidden=wanted)
                    )
        elif path.endswith(".csv"):
            columns = CASE_SCORE_COLUMNS if "scores" in path else CASE_METRIC_COLUMNS
            for i, record in enumerate(read_csv(data, columns)):
                for column, cell in record.items():
                    if column in _NUMERIC_CSV_COLUMNS or not cell:
                        continue
                    if column in {"strata", "candidates"}:
                        out.extend(
                            (path, f"row {i + 1} {column} {hit}")
                            for hit in phi_hits_in_json(json.loads(cell), forbidden=wanted)
                        )
                    else:
                        out.extend(
                            (path, f"row {i + 1} {column}: {hit}")
                            for hit in phi_hits(cell, forbidden=wanted)
                        )
        else:
            out.extend((path, hit) for hit in phi_hits(text, forbidden=wanted))
    return out


def rekeyed_case_rows(
    rows: Sequence[dict[str, Any]], *, report_salt: bytes
) -> tuple[list[list[Any]], dict[str, str], dict[str, str]]:
    """Re-key and pseudonymise per-case rows for export. MOS-EVID-011, MOS-EVID-116.

    Returns `(csv rows, patient map, case map)`. The two maps stay with the ISSUER: they
    are what lets the tenant answer "which of my patients is `pk_...` in that bundle", and
    exporting them would undo the re-keying entirely.

    Case pseudonyms are assigned in the order the rows arrive, which the caller has already
    sorted -- the CSV's row order is part of the bootstrap's reproducibility
    (`MOS-EVID-058`), so it cannot be left to a dict's iteration order.
    """
    patient_map: dict[str, str] = {}
    case_map: dict[str, str] = {}
    out: list[list[Any]] = []
    for row in rows:
        source_case = str(row["case_key"])
        if source_case not in case_map:
            case_map[source_case] = case_pseudonym(len(case_map) + 1)
        source_patient = str(row["patient_key"])
        if source_patient not in patient_map:
            patient_map[source_patient] = rekey_patient(report_salt, source_patient)
        out.append(
            [
                case_map[source_case],
                patient_map[source_patient],
                # MOS-EVID-116: "never by source UIDs". The series handle inside the bundle
                # is the case pseudonym, which keeps the row joinable to case_scores.csv
                # and says nothing about the study it came from.
                case_map[source_case],
                row["metric"],
                "" if row.get("value") is None else repr(float(row["value"])),
                row.get("undefined_reason") or "",
                "true" if row.get("eligible") else "false",
                int(row["gt_voxels"]),
                int(row["pred_voxels"]),
                int(row["intersection_voxels"]),
                repr(float(row["gt_volume_ml"])),
                repr(float(row["pred_volume_ml"])),
                json.dumps(row.get("strata") or {}, sort_keys=True, separators=(",", ":")),
            ]
        )
    return out, patient_map, case_map


def new_export_salt() -> tuple[bytes, str]:
    """`(salt, salt_id)`. The salt never leaves the issuer; the id goes in the report."""
    salt = new_report_salt()
    return salt, report_salt_id(salt)


# =====================================================================================
# 3. Persistence.
# =====================================================================================
def issue_report(
    conn: psycopg.Connection[Any],
    *,
    report: dict[str, Any],
    dsse_envelope: dict[str, Any],
    bundle: Bundle,
    store: ManifestStore,
    report_bucket: str,
    bundle_bucket: str,
    evaluation_run_ids: Sequence[str],
    acceptance_criteria_id: str | None,
    approver_user_id: str | None,
    signer_key_ids: Sequence[str],
    trusted_keys: Sequence[TrustedKey] = (),
    cited_bundle: bytes | None = None,
) -> IssuedReport:
    """Refuse, store the objects, then insert the row. In that order.

    Objects first and the row last, because the row is the thing other tables reference:
    a row pointing at an object that was never written is a dangling reference the gate
    will follow, while an object with no row is garbage a retention sweep collects. If the
    INSERT fails the objects are removed again, which is the same compensating delete
    `medos.evidence.repo.seal_dataset_version` performs for a manifest.
    """
    refusals = _issue_refusals(
        report=report,
        bundle=bundle,
        evaluation_run_ids=evaluation_run_ids,
        trusted_keys=trusted_keys,
        cited_bundle=cited_bundle,
    )
    if refusals:
        raise ReportRefused(refusals)

    tenant_id = _tenant_id(conn)
    report_id = str(report["report_id"])
    payload = report_bytes(report)
    r_key = report_object_key(tenant_id, report_id)
    b_key = bundle_object_key(tenant_id, report_id)
    store.put(report_bucket, r_key, payload)
    store.put(bundle_bucket, b_key, bundle.archive)

    approver = report["approver"]
    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO validation_reports (
                  public_id, tenant_id, kind, schema_version, report_version,
                  subject_kind, subject_id, subject_version, capability_id,
                  criteria_version, evaluation_run_ids, acceptance_criteria_id, verdict,
                  report_bucket, report_object_key, report_digest, envelope_digest,
                  bundle_bucket, bundle_object_key, bundle_digest,
                  signature, signer_key_id, approver, approver_user_id, approver_name,
                  approver_role, issued_at, valid_until, cited_report_digest)
                VALUES (%s, current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s)
                """,
                (
                    report_id,
                    report["kind"],
                    report["schema_version"],
                    int(report["report_version"]),
                    report["subject"]["kind"],
                    report["subject"]["id"],
                    report["subject"]["version"],
                    report["capability"]["id"],
                    int(report["capability"]["criteria_version"]),
                    list(evaluation_run_ids),
                    acceptance_criteria_id,
                    report["verdict"],
                    report_bucket,
                    r_key,
                    report_digest(report),
                    report["applicability_envelope"]["digest"],
                    bundle_bucket,
                    b_key,
                    bundle.digest,
                    json.dumps(dsse_envelope, sort_keys=True).encode("utf-8"),
                    signer_key_ids[0],
                    json.dumps(approver, sort_keys=True),
                    approver_user_id,
                    approver["name"],
                    approver["role"],
                    report["issued_at"],
                    report["valid_until"],
                    (report.get("cites") or {}).get("vendor_report_digest"),
                ),
            )
    except Exception:
        store.delete(report_bucket, r_key)
        store.delete(bundle_bucket, b_key)
        raise

    return IssuedReport(
        report_id=report_id,
        report_digest=report_digest(report),
        bundle_digest=bundle.digest,
        verdict=str(report["verdict"]),
        signer_key_ids=tuple(signer_key_ids),
        bundle=bundle,
    )


def _issue_refusals(
    *,
    report: dict[str, Any],
    bundle: Bundle,
    evaluation_run_ids: Sequence[str],
    trusted_keys: Sequence[TrustedKey],
    cited_bundle: bytes | None,
) -> list[Refusal]:
    refusals: list[Refusal] = []
    for problem in validate_report(report):
        refusals.append(
            Refusal(check_id="MOS-EVID-123", code="schema_violation", message=problem)
        )
    if not evaluation_run_ids:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-065",
                code="no_evaluation_run",
                message="a report cites at least one EvaluationRun; a claim with no run "
                "behind it is the thing chapter 7 exists to make impossible",
            )
        )

    # MOS-EVID-117's half that no CHECK can express. An automated approver MUST NOT be
    # permitted, and the only machine-readable signal for "a human did this" is the
    # identity assurance: a service account's assurance names a client, not a subject.
    assurance = str((report.get("approver") or {}).get("identity_assurance", ""))
    if assurance.startswith(("service_account:", "api_key:", "client_credentials:")):
        refusals.append(
            Refusal(
                check_id="MOS-EVID-117",
                code="automated_approver",
                message="the approver resolves to a service account; an automated "
                "approver MUST NOT be permitted",
                observed=assurance.split(":", 1)[0],
            )
        )

    # The report must verify as exported, before it is recorded as having been issued.
    # Issuing a bundle that does not pass its own seven checks publishes a claim the
    # platform itself cannot confirm.
    if trusted_keys:
        result = verify_archive(bundle.archive, list(trusted_keys))
        if not result.verified:
            failed = next((c for c in result.checks if not c.ok), None)
            refusals.append(
                Refusal(
                    check_id="MOS-EVID-123",
                    code="bundle_does_not_verify",
                    message=(
                        f"the exported bundle fails its own check "
                        f"{failed.number} ({failed.name}): {failed.detail}"
                        if failed
                        else f"the exported bundle does not verify: {result.usage_error}"
                    ),
                )
            )

    # MOS-EVID-128, the half that is only enforceable here: "MUST fail to issue if that
    # report does not verify offline at the site". Offline, at the site, with the site's
    # own trust bundle -- which is why the cited bundle is passed in rather than fetched.
    if report.get("kind") == "site_acceptance":
        cited = (report.get("cites") or {}).get("vendor_report_digest")
        if cited_bundle is None:
            refusals.append(
                Refusal(
                    check_id="MOS-EVID-128",
                    code="cited_report_not_presented",
                    message="a site_acceptance report cites a vendor_evidence report and "
                    "MUST NOT issue until that bundle has been verified offline here",
                )
            )
        else:
            result = verify_archive(cited_bundle, list(trusted_keys))
            if not result.verified:
                refusals.append(
                    Refusal(
                        check_id="MOS-EVID-128",
                        code="cited_report_does_not_verify",
                        message="the cited vendor_evidence bundle does not verify offline "
                        f"(exit {result.exit_code})",
                    )
                )
            else:
                got = (result.report or {}).get("report_digest") or sha256_of(
                    read_bundle(cited_bundle).member("report.json")
                )
                if got != cited:
                    refusals.append(
                        Refusal(
                            check_id="MOS-EVID-128",
                            code="cited_report_digest_mismatch",
                            message="the presented vendor bundle is not the one cited",
                            observed=got,
                            bound=cited,
                        )
                    )
    return refusals


def get_report(conn: psycopg.Connection[Any], public_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM validation_reports WHERE public_id = %s", (public_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def revoke_report(
    conn: psycopg.Connection[Any], *, public_id: str, reason: str
) -> None:
    """MOS-EVID-126: the bytes do not change; `status` does.

    No UPDATE of `report_digest`, `bundle_digest` or `signature` is even attempted -- the
    grant does not permit it and the trigger would refuse it. That is what makes "this
    evidence was once accepted and has since been withdrawn" an auditable fact rather than
    a hole: the original bundle still verifies offline, forever, and the platform reports
    it as REVOKED when asked online.
    """
    if not reason or len(reason.strip()) < 10:
        raise ReportRefused([
            Refusal(
                check_id="MOS-EVID-126",
                code="revocation_reason_missing",
                message="revocation MUST carry a reason; 'revoked' is not one",
            )
        ])
    n = conn.execute(
        "UPDATE validation_reports SET status = 'REVOKED', revocation_reason = %s "
        "WHERE public_id = %s AND status <> 'REVOKED'",
        (reason, public_id),
    ).rowcount
    if n == 0 and get_report(conn, public_id) is None:
        raise ReportRefused([
            Refusal(check_id="MOS-EVID-126", code="report_not_found",
                    message=f"no report {public_id!r} in this tenant")
        ])


def supersede_report(
    conn: psycopg.Connection[Any], *, public_id: str, by_public_id: str
) -> None:
    """Point an older report at the one that replaced it. Bytes unchanged."""
    conn.execute(
        """
        UPDATE validation_reports SET status = 'SUPERSEDED', superseded_by = (
          SELECT id FROM validation_reports WHERE public_id = %s)
        WHERE public_id = %s
        """,
        (by_public_id, public_id),
    )


def mark_degraded(conn: psycopg.Connection[Any], *, public_id: str) -> None:
    """MOS-STORE-341 step 9: an erasure removed the cohort, so the run is not re-runnable.

    The report stays readable, signed and valid as a historical claim. Degrading it is not
    revoking it: the claim was true when it was made and the evidence for it is gone, which
    are two different facts and a site needs to be able to tell them apart.
    """
    conn.execute(
        "UPDATE validation_reports SET reproducibility_status = 'degraded' "
        "WHERE public_id = %s",
        (public_id,),
    )


def revoke_reports_citing_runs(
    conn: psycopg.Connection[Any], *, run_ids: Sequence[str], reason: str
) -> int:
    """MOS-EVID-014 / MOS-STORE-301a: the DEFECTIVE cascade, as one statement.

    "Marking a `dataset_versions` row DEFECTIVE MUST, in the same transaction, set
    `validation_reports.status = 'REVOKED'` ... The cascade is a transaction, not a
    background job: a defective cohort must never leave a live report standing on it."
    So this takes the caller's connection and does NOT open its own transaction -- the
    caller's is the one that must contain it.
    """
    return conn.execute(
        "UPDATE validation_reports SET status = 'REVOKED', revocation_reason = %s "
        "WHERE evaluation_run_ids && %s::uuid[] AND status <> 'REVOKED'",
        (reason, list(run_ids)),
    ).rowcount


def _tenant_id(conn: psycopg.Connection[Any]) -> str:
    """The bound tenant, read from the DATABASE. Never from a caller argument.

    `medos.db.tenancy` owns the GUC and a test asserts exactly one writer of it. Reading
    it back here rather than accepting a parameter means the object key and the row can
    never disagree about which tenant this is.
    """
    row = conn.execute("SELECT current_tenant_id() AS t").fetchone()
    return str(row["t"] if isinstance(row, dict) else row[0])


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
