# SPDX-License-Identifier: Apache-2.0
"""Applicability envelopes, RUO marking and `ResultReview`. Migration 0010.

docs/spec/15-delivery.md section 15.1.2, the 0.2.0 row, names three chapter 9 items beside
the evidence plane: "applicability envelopes; RUO marking; `ResultReview`", and one gate
check over them -- `ruo-marking`: "in `clinical_use_mode: research_only` the writer refuses
to emit an unmarked object".

WHAT THIS FILE IS TRYING TO CATCH, in the order the release brief names them
----------------------------------------------------------------------------
1. A study outside a declared envelope that is analysed anyway, or that terminates FAILED
   instead of REJECTED. `MOS-DATA-077` requires an explicitly requested job to exist and
   end `REJECTED` with `outside_applicability_envelope`; `MOS-EXEC-014` is why the
   distinction is release-gated. The envelope gate is exercised through the real worker
   step against a real database, not through the evaluator alone, because the defect this
   is guarding against is "the evaluator was right and nothing called it".
2. A `MARGINAL` study silently promoted to `IN`. `MOS-EVID-096`'s "the study's zone is the
   worst zone across all constraints" and "a missing attribute evaluates to `MARGINAL` ...
   never to `IN`" are both asserted, and so is the consequence: the provenance record for a
   flagged study must NOT say `in_envelope: true`.
3. An unmarked object reaching the PACS. Every marker of `MOS-IMG-133`/`136`/`137` is
   removed in turn from an object built by the real writer helpers, and each removal must
   raise. The check must also be un-disableable (`MOS-SAFE-039`: "There is no configuration
   flag that disables it"), which is grepped for.
4. A `ResultReview` row that can be edited after submission, or that gates visibility.
   `MOS-SAFE-060` puts the append-only rule in the database, so the assertions go through
   SQL. `MOS-SAFE-063`'s "zero jobs, zero STOW" test is here because that requirement asks
   chapter 14 for it by name.
5. Chapter 16's OQ-01 resolved by accident. `mandatory_pre_publication` must be refused,
   and nothing in the review path may gate storage or visibility.

SKIP DISCIPLINE: `tests/_support/skips.py` only. No raw `pytest.skip` in this file; the
module sits under `tests/integration`, whose `postgres` dependency `tests/_support/stack.py`
probes under `--require-stack`.

PHI: every DICOM object this file synthesises carries a fabricated PatientID and no
PatientName. Nothing here prints one, and `test_study_attributes_carry_no_phi` asserts that
the attribute record cannot.
"""

from __future__ import annotations

import inspect
import re
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pydicom
import pytest
from medos.db import repo
from medos.db.conn import apply_schema
from medos.db.migrate import discover
from medos.db.tenancy import DEFAULT_TENANT_ID, TENANT_GUC, bind_current_tenant
from medos.safety import repo as safety_repo
from medos.safety.attributes import (
    attributes_from_paths,
    contrast_phase,
    kernel_class,
    normalise_body_part,
    patient_age_years,
)
from medos.safety.declarations import (
    SLICE_SERVICE_ID,
    SLICE_SERVICE_VERSION,
    consistency_violations,
    slice_envelope,
)
from medos.safety.envelope import (
    REASON_CODES,
    ApplicabilityEnvelope,
    EnvelopeDeclarationError,
    EnvelopeViolation,
    evaluate,
)
from medos.safety.marking import (
    MarkingAbsent,
    apply_research_marking,
    assert_marked,
    marking_violations,
)
from medos.safety.policy import DeploymentSafetyPolicy, ReviewModeReserved
from medos.safety.review import (
    EVENT_NAMES,
    PERMISSIONS,
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransition,
    SubmissionInvalid,
    may_emit_verified_sr,
    next_state,
    reviewer_class_for,
    validate_submission,
)
from medos.writer.identity import DEPLOYMENT_ID, ai_series_description, apply_ai_marking
from psycopg.errors import CheckViolation, UniqueViolation
from psycopg.rows import dict_row
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

from tests._support.skips import skip_infra

SAFETY_TABLES = ("result_reviews", "envelope_decisions", "applicability_envelopes")
OPERATOR = "11111111-1111-1111-1111-111111111111"

RATIONALE = (
    "Right-sided volume includes subdiaphragmatic ascites; corrected after manual "
    "exclusion of the infradiaphragmatic component."
)


# =====================================================================================
# Fixtures
# =====================================================================================
@pytest.fixture()
def safety_db(pg_dsn: str) -> Iterator[psycopg.Connection[Any]]:
    """One connection with the default tenant bound and this release's tables emptied.

    `jobs` and `results` are truncated too: the envelope gate writes an
    `envelope_decisions` row that references a job, and the review tests need a `results`
    row to hang a review off. TRUNCATE rather than DELETE for the reason the evidence and
    weeks 1-2 fixtures give -- every table here carries a BEFORE DELETE trigger that
    raises, and TRUNCATE fires no row-level triggers, so the append-only guarantee holds
    for application code while the harness can still start clean.
    """
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    conn.execute("SELECT set_config(%s, %s, false)", (TENANT_GUC, DEFAULT_TENANT_ID))
    conn.execute(
        f"TRUNCATE {', '.join(SAFETY_TABLES)}, jobs, job_queue, job_events, job_steps, "
        "job_series, results, result_measurements, result_dicom_objects CASCADE"
    )
    conn.execute("UPDATE tenants SET marginal_policy = 'flag'")
    conn.commit()
    token = bind_current_tenant(DEFAULT_TENANT_ID)
    try:
        yield conn
    finally:
        from medos.db.tenancy import reset_current_tenant

        reset_current_tenant(token)
        conn.close()


def _ct_instance(
    *,
    directory: Path,
    index: int,
    study_uid: str,
    series_uid: str,
    slice_thickness: float = 1.0,
    pixel_spacing: float = 0.7,
    kernel: str | None = "B31f",
    manufacturer: str = "SIEMENS",
    body_part: str | None = "CHEST",
    patient_age: str | None = "064Y",
    contrast_agent: str | None = None,
    series_description: str = "Thorax 1.0 B31f",
) -> Path:
    """One header-only CT instance. No PixelData: the envelope gate is metadata-only.

    `MOS-DATA-058` requires triage to pull no pixels, and `attributes_from_paths` reads
    with `stop_before_pixels=True`; writing a file with no pixel data is the strongest
    possible assertion that the gate does not need them.
    """
    ds = Dataset()
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = generate_uid()
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.Modality = "CT"
    ds.PatientID = "SYNTH-0001"  # fabricated; there is no PatientName at all
    ds.SliceThickness = slice_thickness
    ds.PixelSpacing = [pixel_spacing, pixel_spacing]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.ImagePositionPatient = [-150.0, -150.0, float(index) * slice_thickness]
    ds.Manufacturer = manufacturer
    ds.SeriesDescription = series_description
    if kernel is not None:
        ds.ConvolutionKernel = kernel
    if body_part is not None:
        ds.BodyPartExamined = body_part
    if patient_age is not None:
        ds.PatientAge = patient_age
    if contrast_agent is not None:
        ds.ContrastBolusAgent = contrast_agent

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = meta
    ds.is_little_endian = True
    ds.is_implicit_VR = False

    path = directory / f"{index:04d}.dcm"
    ds.save_as(str(path), enforce_file_format=True)
    return path


def _executable_source(path: Path) -> str:
    """A module's CODE, with docstrings and comments removed.

    Three assertions below scan a module for a token it must not contain -- an off switch,
    a hardcoded role key, an environment override. Every one of those modules explains in
    its own docstring exactly which token it refuses to have, which is the right thing for
    the module to do and would make a naive `in source` scan fail on the explanation. A
    scan that punishes a file for documenting its own prohibition trains the next author to
    delete the sentence.

    Comment and string literals are stripped with `tokenize`, so this is the parser's
    opinion about what is code, not a regex's.
    """
    import io
    import tokenize

    kept: list[str] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(io.BytesIO(handle.read()).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    return " ".join(kept)


def _synthetic_series(
    directory: Path, *, n: int = 60, **kwargs: Any
) -> tuple[str, str, list[Path]]:
    directory.mkdir(parents=True, exist_ok=True)
    study_uid = generate_uid()
    series_uid = generate_uid()
    paths = [
        _ct_instance(
            directory=directory, index=i, study_uid=study_uid, series_uid=series_uid, **kwargs
        )
        for i in range(n)
    ]
    return study_uid, series_uid, paths


# =====================================================================================
# 1. The migration
# =====================================================================================
@pytest.fixture()
def own_empty_dsn() -> Iterator[str]:
    """A brand-new empty database THIS MODULE owns, for the applies-from-empty test.

    Deliberately not the session-scoped `pristine_dsn` fixture. That database is created
    once per session with no schema, and `apply_schema` is not idempotent -- the first
    `CREATE TYPE job_state` wins and the second run gets `DuplicateObject`. Several
    migrations in this release block were authored in parallel and more than one of them
    wants an applies-from-empty test, so sharing one single-use database makes whichever
    test happens to run second fail for a reason that has nothing to do with the migration
    it is checking. Minting a database here costs about a second and takes this file out of
    that contention entirely.
    """
    from tests.integration.conftest import ADMIN_URL, _admin_conn, _swap_dbname

    name = f"medos_safety_{secrets.token_hex(6)}"
    try:
        admin = _admin_conn()
    except psycopg.OperationalError as exc:  # pragma: no cover
        skip_infra(
            f"no Postgres at {ADMIN_URL.rsplit('@', 1)[-1]}: {exc}",
            dependency="postgres",
        )
    with admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _swap_dbname(ADMIN_URL, name)
    finally:
        with _admin_conn() as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.mark.slow
def test_safety_migration_applies_from_empty(own_empty_dsn: str) -> None:
    """0010 applies onto an empty database and leaves the structure it claims.

    The previous release block lost a day to a migration set that no longer applied from
    empty, so this builds a brand-new database rather than inspecting the one the other
    tests share, and asserts the invariants rather than trusting that `CREATE TABLE`
    returning no error means the schema is right.
    """
    with psycopg.connect(own_empty_dsn, autocommit=True, row_factory=dict_row) as conn:
        apply_schema(conn)

        versions = {
            r["version"]
            for r in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        assert "0010_safety" in versions, (
            f"0010 did not run; the ledger holds {sorted(versions)}"
        )

        for table in SAFETY_TABLES:
            row = conn.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = %s AND n.nspname = 'public'",
                (table,),
            ).fetchone()
            assert row is not None, f"{table} was not created"
            # MOS-STORE-223/224: ENABLE alone exempts the owner, and the owner is the role
            # a migration runs as. FORCE is what makes the policy bind everyone.
            assert row["relrowsecurity"] and row["relforcerowsecurity"], (
                f"{table} is not FORCE ROW LEVEL SECURITY"
            )

        # MOS-SAFE-060: no DELETE anywhere, and no TABLE-level UPDATE on result_reviews.
        deletes = conn.execute(
            "SELECT table_name FROM information_schema.table_privileges "
            "WHERE grantee = 'medicalos_app' AND privilege_type = 'DELETE' "
            "AND table_name = ANY(%s)",
            (list(SAFETY_TABLES),),
        ).fetchall()
        assert deletes == [], f"medicalos_app holds DELETE on {deletes}"

        table_update = conn.execute(
            "SELECT 1 FROM information_schema.table_privileges "
            "WHERE grantee = 'medicalos_app' AND privilege_type = 'UPDATE' "
            "AND table_name = 'result_reviews'"
        ).fetchone()
        assert table_update is None, (
            "medicalos_app holds table-level UPDATE on result_reviews; MOS-SAFE-060 "
            "requires the column-level grant so that result_id and round cannot be moved"
        )

        # MOS-EVID-101's tenant setting exists with the requirement's default.
        default = conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'tenants' AND column_name = 'marginal_policy'"
        ).fetchone()
        assert default is not None and "flag" in str(default["column_default"])


def test_migration_number_is_0010_and_nothing_was_renumbered() -> None:
    """This block's allocated number is 0010, and the applied set is untouched.

    Two agents independently choosing 0004 cost a database rebuild last block, so the
    assertion is cheap and the failure mode is expensive. `0001_baseline` is `schema.sql`
    itself and has no file under `migrations/`; the numbered files start at 0002.
    """
    versions = [m.version for m in discover()]
    assert "0010_safety" in versions
    assert versions == sorted(versions), f"migrations are not lexically ordered: {versions}"
    for expected in (
        "0002_tenancy",
        "0003_audit_provenance",
        "0004_auth",
        "0005_gateway",
        "0006_evidence",
    ):
        assert expected in versions, f"{expected} was renumbered or removed"
    # Exactly one file claims 0010, and it is this block's.
    tenth = [v for v in versions if v.startswith("0010")]
    assert tenth == ["0010_safety"], f"0010 is claimed by {tenth}"


# =====================================================================================
# 2. The declaration -- MOS-EVID-095, MOS-EVID-103
# =====================================================================================
def test_the_specification_example_envelope_loads() -> None:
    """`MOS-EVID-095`'s own worked manifest, parsed and content-addressed.

    If the requirement's example does not load, the loader is wrong about the schema and
    every envelope a publisher writes from the specification will be refused.
    """
    manifest = {
        "apiVersion": "medicalos.io/v1",
        "kind": "ApplicabilityEnvelope",
        "metadata": {
            "subject_kind": "service_version",
            "subject_id": "pulmoai.effusion",
            "subject_version": "2.1.0",
            "version": 4,
            "derived_from_evaluation_run": "evr_01JB4Q8T5XN7M2VDKC3PZR9HAE",
            "derivation": "percentile_1_99",
        },
        "spec": {
            "constraints": [
                {"attribute": "slice_thickness_mm", "type": "range", "min": 0.625,
                 "max": 3.0, "marginal_max": 5.0},
                {"attribute": "pixel_spacing_mm_max", "type": "range", "min": 0.488,
                 "max": 0.977, "marginal_max": 1.2},
                {"attribute": "z_coverage_mm", "type": "range", "min": 180.0,
                 "max": 450.0, "marginal_min": 150.0},
                {"attribute": "instance_count", "type": "range", "min": 90, "max": 750,
                 "marginal_max": 1200},
                {"attribute": "patient_age_years", "type": "range", "min": 18, "max": 95},
                {"attribute": "convolution_kernel_class", "type": "enum_in",
                 "values": ["soft", "standard"], "marginal_values": ["sharp"]},
                {"attribute": "manufacturer", "type": "enum_in",
                 "values": ["SIEMENS", "GE MEDICAL SYSTEMS", "Philips",
                            "CANON MEDICAL SYSTEMS"],
                 "marginal_values_allowed": True},
                {"attribute": "body_part_examined", "type": "enum_in",
                 "values": ["CHEST", "THORAX"]},
            ],
            "marginal_policy_default": "flag",
        },
    }
    envelope = ApplicabilityEnvelope.from_manifest(manifest)
    assert envelope.version == 4
    assert len(envelope.constraints) == 8
    # MOS-EVID-007: `"sha256:" + lowercase_hex`, the form the `sha256_digest` domain
    # enforces and the form chapter 7 section 7.12.1's ValidationReport cites.
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", envelope.digest)
    # Content-addressed: the digest is a function of the declaration and nothing else, so
    # re-parsing the same document yields the same address on any machine.
    assert ApplicabilityEnvelope.from_manifest(manifest).digest == envelope.digest
    # Defect D3: chapter 7's lower-case categorical values normalise onto chapter 3's
    # derived vocabulary rather than silently never matching.
    kernel = next(c for c in envelope.constraints
                  if c.attribute == "convolution_kernel_class")
    assert kernel.values == ("SOFT", "STANDARD")


def test_a_constraint_with_no_reason_code_is_refused_at_load() -> None:
    """`MOS-EVID-103`'s set is closed, so an attribute outside it cannot be declared.

    The alternative is a rejection with no machine-readable reason, which CONTRACT.md
    section 3 forbids outright. `MOS-DATA-073` takes the same posture for a `SeriesSelector`
    with an unknown `match` key: refuse at registration, not at run time.
    """
    with pytest.raises(EnvelopeDeclarationError, match="reason_code"):
        ApplicabilityEnvelope.from_manifest(
            {
                "subject_kind": "service_version",
                "subject_id": "x",
                "subject_version": "1",
                "version": 1,
                "constraints": [{"attribute": "kvp", "type": "range", "min": 70}],
            }
        )


def test_a_categorical_value_outside_chapter_3s_vocabulary_is_refused() -> None:
    """Defect D3, the half that must NOT be papered over.

    `MOS-EVID-095`'s example writes `contrast_phase: [non_contrast, venous]`;
    `MOS-DATA-060.5` derives `NONE` / `ARTERIAL` / `PORTAL_VENOUS` / `DELAYED` /
    `PULMONARY_ARTERIAL` / `UNKNOWN`. A case-insensitive best-effort match would accept
    `venous`, match nothing, and reject every study that reached it. The loader refuses and
    names the permitted set.
    """
    with pytest.raises(EnvelopeDeclarationError) as exc:
        ApplicabilityEnvelope.from_manifest(
            {
                "subject_kind": "service_version",
                "subject_id": "x",
                "subject_version": "1",
                "version": 1,
                "constraints": [
                    {"attribute": "contrast_phase", "type": "enum_in",
                     "values": ["venous"]}
                ],
            }
        )
    assert "PORTAL_VENOUS" in str(exc.value)


def test_an_envelope_cannot_constrain_one_attribute_twice() -> None:
    """`MOS-EVID-096` evaluates an attribute to EXACTLY one zone."""
    with pytest.raises(EnvelopeDeclarationError, match="twice"):
        ApplicabilityEnvelope.from_manifest(
            {
                "subject_kind": "service_version",
                "subject_id": "x",
                "subject_version": "1",
                "version": 1,
                "constraints": [
                    {"attribute": "instance_count", "type": "range", "min": 1},
                    {"attribute": "instance_count", "type": "range", "min": 2},
                ],
            }
        )


def test_a_marginal_band_narrower_than_the_validated_band_is_refused() -> None:
    """A tolerance that tolerates less than the claim is a typo, and it rejects studies
    the publisher declared valid. Refused where it is written, not discovered in traffic."""
    with pytest.raises(EnvelopeDeclarationError, match="narrower"):
        ApplicabilityEnvelope.from_manifest(
            {
                "subject_kind": "service_version",
                "subject_id": "x",
                "subject_version": "1",
                "version": 1,
                "constraints": [
                    {"attribute": "slice_thickness_mm", "type": "range", "max": 3.0,
                     "marginal_max": 2.0},
                ],
            }
        )


def test_every_reason_code_is_in_the_closed_lowercase_dotted_set() -> None:
    """`MOS-EVID-103`: "a closed set in 0.2.0, in lowercase dotted form"."""
    expected = {
        "envelope.slice_thickness_out_of_range",
        "envelope.pixel_spacing_out_of_range",
        "envelope.coverage_insufficient",
        "envelope.instance_count_out_of_range",
        "envelope.kernel_not_supported",
        "envelope.manufacturer_unvalidated",
        "envelope.contrast_phase_unsupported",
        "envelope.body_part_mismatch",
        "envelope.age_out_of_range",
    }
    assert set(REASON_CODES.values()) == expected
    for code in REASON_CODES.values():
        assert re.fullmatch(r"envelope\.[a-z_]+", code), code


# =====================================================================================
# 3. The three zones -- MOS-EVID-096, MOS-EVID-101
# =====================================================================================
def test_worst_zone_wins_and_a_missing_attribute_is_never_in() -> None:
    """`MOS-EVID-096`, both sentences, on one envelope.

    The second is the one that is easy to get backwards: an absent `ConvolutionKernel` is
    not evidence that the kernel was fine.
    """
    envelope = slice_envelope()
    base = {
        "slice_thickness_mm": 1.0,
        "pixel_spacing_mm_max": 0.7,
        "z_coverage_mm": 300.0,
        "instance_count": 300,
        "patient_age_years": 64,
        "manufacturer": "SIEMENS",
        "convolution_kernel_class": "STANDARD",
        "contrast_phase": "NONE",
        "body_part_examined": "CHEST",
    }
    assert evaluate(envelope, base).zone == "IN"

    # One MARGINAL attribute drags the whole study to MARGINAL.
    marginal = evaluate(envelope, {**base, "convolution_kernel_class": "SHARP"})
    assert marginal.zone == "MARGINAL"
    assert marginal.outcome == "proceed_flagged"
    assert marginal.in_envelope is False, (
        "a MARGINAL study a flag policy allowed through is OUTSIDE the envelope and was "
        "permitted anyway; MOS-SAFE-083's in_envelope must not claim otherwise"
    )

    # One OUT attribute beats any number of MARGINAL ones.
    out = evaluate(
        envelope,
        {**base, "convolution_kernel_class": "SHARP", "slice_thickness_mm": 8.0},
    )
    assert out.zone == "OUT"
    assert out.reason_code == "envelope.slice_thickness_out_of_range"

    # Absent attribute -> MARGINAL / attribute_absent, never IN.
    absent = evaluate(envelope, {**base, "convolution_kernel_class": None})
    assert absent.zone == "MARGINAL"
    verdict = next(v for v in absent.verdicts
                   if v.attribute == "convolution_kernel_class")
    assert verdict.detail == "attribute_absent"
    assert verdict.reason_code == "envelope.attribute_absent"


def test_the_tenant_marginal_policy_decides_what_marginal_does() -> None:
    """`MOS-EVID-101`'s two MARGINAL rows. `OUT` is not a tenant decision and never is."""
    envelope = slice_envelope()
    attributes = {
        "slice_thickness_mm": 1.0, "pixel_spacing_mm_max": 0.7, "z_coverage_mm": 300.0,
        "instance_count": 300, "patient_age_years": 64, "manufacturer": "SIEMENS",
        "convolution_kernel_class": "SHARP", "contrast_phase": "NONE",
        "body_part_examined": "CHEST",
    }
    flagged = evaluate(envelope, attributes, marginal_policy="flag")
    assert (flagged.outcome, flagged.rejected) == ("proceed_flagged", False)

    strict = evaluate(envelope, attributes, marginal_policy="reject")
    assert (strict.outcome, strict.rejected) == ("rejected", True)
    assert strict.reason_code == "envelope.kernel_not_supported"

    # OUT rejects under either policy.
    for policy in ("flag", "reject"):
        out = evaluate(
            envelope, {**attributes, "body_part_examined": "HEAD"}, marginal_policy=policy
        )
        assert out.rejected is True, f"an OUT study proceeded under marginal_policy={policy}"


def test_the_rejection_object_has_every_member_MOS_EVID_102_requires() -> None:
    """One entry per violated constraint, with `observed` and `bound` as real values.

    `MOS-DATA-079`: "`observed` and `expected` MUST contain real values from the triage
    record and the selector, not a rendered sentence. The UI renders the sentence; the API
    returns the data." Both are present; the sentence is IN ADDITION.
    """
    envelope = slice_envelope()
    verdict = evaluate(
        envelope,
        {
            "slice_thickness_mm": 8.0, "pixel_spacing_mm_max": 0.7,
            "z_coverage_mm": 300.0, "instance_count": 300, "patient_age_years": 64,
            "manufacturer": "SIEMENS", "convolution_kernel_class": "SOFT",
            "contrast_phase": "NONE", "body_part_examined": "CHEST",
        },
    )
    violations = verdict.violations()
    assert len(violations) == 1
    entry = violations[0]
    assert entry["class"] == "clinical_rejection"
    assert entry["code"] == "ENVELOPE_VIOLATION"
    assert entry["reason_code"] == "envelope.slice_thickness_out_of_range"
    assert entry["attribute"] == "slice_thickness_mm"
    assert entry["observed"] == 8.0
    assert entry["bound"]["max"] == 3.0
    assert entry["subject"] == {"id": SLICE_SERVICE_ID, "version": SLICE_SERVICE_VERSION}
    assert entry["envelope_version"] == 1
    assert isinstance(entry["message"], str) and entry["message"]


def test_a_rejecting_verdict_raises_a_clinical_rejection_not_a_failure() -> None:
    """`MOS-EVID-102` / CONTRACT.md section 3 / `MOS-EXEC-014`.

    The whole point of the type hierarchy: `EnvelopeViolation` is a `ClinicalRejection`, so
    the runner takes T7 and writes `REJECTED`, the problem body carries
    `class: clinical_rejection`, HTTP 422, and it is never retried. A radiologist who sees
    a red error where the truth is "this study is 8 mm and the service is validated to 3"
    chases an IT ticket.
    """
    from medos.core.errors import ClinicalRejection
    from medos.worker.steps import reject_reason_code

    envelope = slice_envelope()
    verdict = evaluate(
        envelope,
        {"slice_thickness_mm": 8.0, "instance_count": 300, "patient_age_years": 64,
         "manufacturer": "SIEMENS", "convolution_kernel_class": "SOFT",
         "contrast_phase": "NONE", "body_part_examined": "CHEST",
         "pixel_spacing_mm_max": 0.7, "z_coverage_mm": 300.0},
    )
    with pytest.raises(EnvelopeViolation) as exc:
        verdict.raise_if_rejected()

    error = exc.value
    assert isinstance(error, ClinicalRejection)
    assert error.problem_class == "clinical_rejection"
    assert error.http_status == 422
    assert error.retryable is False
    # And it lands on chapter 5's closed job-level code, not on `input_constraint_unmet`.
    assert reject_reason_code(error, "envelope_check") == "outside_applicability_envelope"
    assert error.detail["violations"][0]["reason_code"] == (
        "envelope.slice_thickness_out_of_range"
    )


def test_not_validated_for_conditions_are_all_out() -> None:
    """`MOS-EVID-099`: "CI MUST assert this and fail the ServiceVersion publish on
    inconsistency." The capability metadata names paediatric patients and thick recons."""
    failures = consistency_violations(
        {
            "paediatric_under_18": {
                "slice_thickness_mm": 1.0, "pixel_spacing_mm_max": 0.7,
                "z_coverage_mm": 300.0, "instance_count": 300, "patient_age_years": 8,
                "manufacturer": "SIEMENS", "convolution_kernel_class": "SOFT",
                "contrast_phase": "NONE", "body_part_examined": "CHEST",
            },
            "thick_recon_over_5mm": {
                "slice_thickness_mm": 6.0, "pixel_spacing_mm_max": 0.7,
                "z_coverage_mm": 300.0, "instance_count": 300, "patient_age_years": 64,
                "manufacturer": "SIEMENS", "convolution_kernel_class": "SOFT",
                "contrast_phase": "NONE", "body_part_examined": "CHEST",
            },
            "non_thoracic": {
                "slice_thickness_mm": 1.0, "pixel_spacing_mm_max": 0.7,
                "z_coverage_mm": 300.0, "instance_count": 300, "patient_age_years": 64,
                "manufacturer": "SIEMENS", "convolution_kernel_class": "SOFT",
                "contrast_phase": "NONE", "body_part_examined": "HEAD",
            },
        }
    )
    assert failures == [], "\n".join(failures)


def test_the_evidence_plane_reads_the_same_declaration() -> None:
    """One envelope, two consumers. `MOS-TRAIN-090`'s C5 check reads the reduced shape.

    `medos.evidence.stratification.stratification_report` asks "does the cohort hold >= 20
    patients in every cell the declared envelope marks IN", against
    `{"categorical": {...}, "numeric": {...}}`. That projection lives on the declaration so
    a publisher never maintains the reduced form by hand -- two declarations agree until the
    day they do not, and that day a seal passes C5 against a cohort that does not cover the
    envelope the runtime is enforcing.
    """
    shape = slice_envelope().to_stratification_shape()
    assert shape["categorical"]["body_part_examined"] == ["CHEST"]
    assert shape["numeric"]["slice_thickness_mm"] == {"min": 0.5, "max": 3.0}
    # A marginal band is not a cell a cohort must cover, so it is absent from the reduction.
    assert "marginal_max" not in shape["numeric"]["slice_thickness_mm"]


# =====================================================================================
# 4. Attribute derivation -- MOS-DATA-060
# =====================================================================================
def test_kernel_class_follows_MOS_DATA_060_2_and_absent_is_not_unknown() -> None:
    """The distinction the function exists to get right.

    `None` (the attribute is absent) evaluates MARGINAL/`attribute_absent`;
    `"UNKNOWN"` (declared and unmapped) is an observed value that lands OUT. Collapsing
    them reports an unrecognised photon-counting kernel as a metadata gap rather than as an
    unvalidated reconstruction.
    """
    assert kernel_class("SIEMENS", "B31f") == "SOFT"
    assert kernel_class("SIEMENS", "B45f") == "STANDARD"
    assert kernel_class("SIEMENS", "B70f") == "SHARP"
    assert kernel_class("SIEMENS", "Bl57") == "SHARP"  # the explicit list, not the regex
    assert kernel_class("GE MEDICAL SYSTEMS", "LUNG") == "SHARP"
    assert kernel_class("Philips", "YB") == "SHARP"
    assert kernel_class("CANON MEDICAL SYSTEMS", "FC51") == "SHARP"
    assert kernel_class("SIEMENS", "QQ99") == "UNKNOWN"
    assert kernel_class("SIEMENS", None) is None
    assert kernel_class("SIEMENS", "") is None


def test_body_part_and_age_and_phase_follow_chapter_3() -> None:
    assert normalise_body_part("thorax") == "CHEST"
    assert normalise_body_part(" Lung ") == "CHEST"
    assert normalise_body_part("ABDOMEN") == "ABDOMEN"  # unmapped is kept verbatim
    assert normalise_body_part("") is None

    # MOS-DATA-059: "parsed from the `nnnY` form, null otherwise". 45 MONTHS is not 45.
    assert patient_age_years("064Y") == 64
    assert patient_age_years("045M") is None
    assert patient_age_years(None) is None

    # MOS-DATA-060.5: NONE when no agent; the regex set otherwise; UNKNOWN on no match.
    assert contrast_phase(contrast_agent=None, series_description="portal venous",
                          protocol_name=None) == ("NONE", None)
    assert contrast_phase(contrast_agent="OMNIPAQUE",
                          series_description="Thorax portal venous 1.0",
                          protocol_name=None) == ("PORTAL_VENOUS", "series_description")
    assert contrast_phase(contrast_agent="OMNIPAQUE", series_description="Thorax 1.0",
                          protocol_name=None) == ("UNKNOWN", None)


def test_study_attributes_carry_no_phi(tmp_path: Path) -> None:
    """CONTRACT.md section 11 and `MOS-DATA-065`, asserted rather than asserted-in-prose.

    `series_description` and `protocol_name` are READ -- `MOS-DATA-060.5` derives the
    contrast phase from them by regex -- and MUST NOT be returned: the record goes into
    `envelope_decisions.observed` and into `job_steps.detail`, and a description is free
    text a technologist typed.
    """
    _study, _series, paths = _synthetic_series(
        tmp_path / "phi", n=8,
        series_description="Mr Smith rescan 1.0 B31f portal venous",
        contrast_agent="OMNIPAQUE",
    )
    attributes = attributes_from_paths(paths)
    rendered = repr(attributes.as_mapping())
    assert "Smith" not in rendered
    assert "SYNTH-0001" not in rendered  # the PatientID is never carried either
    # The FIELD NAME that matched is recorded; the text that matched is not.
    assert attributes.contrast_phase == "PORTAL_VENOUS"
    assert attributes.contrast_phase_matched_on == "series_description"


def test_attributes_are_derived_without_reading_pixel_data(tmp_path: Path) -> None:
    """The instances this test writes have no `PixelData` at all, and the derivation works.

    `MOS-DATA-058`'s "no pixel pull at triage" as a structural property: an envelope
    decision that needed pixels would fail on these files.
    """
    _study, _series, paths = _synthetic_series(tmp_path / "nopixels", n=40)
    attributes = attributes_from_paths(paths)
    assert attributes.instance_count == 40
    assert attributes.slice_thickness_mm == 1.0
    assert attributes.pixel_spacing_mm_max == pytest.approx(0.7)
    # z_extent = max(proj) - min(proj) + slice_thickness  (MOS-DATA-060.3)
    assert attributes.z_coverage_mm == pytest.approx(39.0 + 1.0)
    assert attributes.convolution_kernel_class == "SOFT"
    assert attributes.body_part_examined == "CHEST"
    assert attributes.patient_age_years == 64


# =====================================================================================
# 5. The envelope in the database -- immutability, decisions, the export
# =====================================================================================
def test_a_published_envelope_cannot_be_edited_or_deleted(
    safety_db: psycopg.Connection[Any],
) -> None:
    """Chapter 7 table 7.1: "versioned, immutable per version".

    In the DATABASE, not in application code, for `MOS-EVID-013`'s reason applied to this
    entity: an operator who can edit a published envelope in place can widen a validated
    range without an `EvaluationRun`, which is the exact move `MOS-EVID-098` refuses.
    """
    row = safety_repo.declare_envelope(safety_db, slice_envelope(), declared_by=OPERATOR)
    safety_db.commit()

    with pytest.raises(psycopg.DatabaseError, match="immutable"):
        safety_db.execute(
            "UPDATE applicability_envelopes SET marginal_policy_default = 'reject' "
            "WHERE id = %s",
            (row.id,),
        )
    safety_db.rollback()

    with pytest.raises(psycopg.DatabaseError, match="immutable"):
        safety_db.execute("DELETE FROM applicability_envelopes WHERE id = %s", (row.id,))
    safety_db.rollback()


def test_declaring_the_same_envelope_twice_is_idempotent_but_changing_it_is_not(
    safety_db: psycopg.Connection[Any],
) -> None:
    """Re-declaring a byte-identical envelope carries no information; changing one at the
    same version is `MOS-EVID-098`'s widening move and must not be possible."""
    first = safety_repo.declare_envelope(safety_db, slice_envelope(), declared_by=OPERATOR)
    second = safety_repo.declare_envelope(safety_db, slice_envelope(), declared_by=OPERATOR)
    assert first.id == second.id
    safety_db.commit()

    widened = ApplicabilityEnvelope.from_manifest(
        {
            "subject_kind": "service_version",
            "subject_id": SLICE_SERVICE_ID,
            "subject_version": SLICE_SERVICE_VERSION,
            "version": 1,  # the SAME version, a DIFFERENT claim
            "constraints": [
                {"attribute": "slice_thickness_mm", "type": "range", "min": 0.5,
                 "max": 10.0},
            ],
        }
    )
    with pytest.raises(UniqueViolation):
        safety_repo.declare_envelope(safety_db, widened, declared_by=OPERATOR)
    safety_db.rollback()


def test_resolve_returns_the_highest_version(safety_db: psycopg.Connection[Any]) -> None:
    """No `is_active` flag: "which envelope was this study judged against" must never be
    ambiguous, and a boolean maintained in a second statement is one day true on two rows."""
    safety_repo.declare_envelope(safety_db, slice_envelope(), declared_by=OPERATOR)
    narrowed = ApplicabilityEnvelope.from_manifest(
        {
            "subject_kind": "service_version",
            "subject_id": SLICE_SERVICE_ID,
            "subject_version": SLICE_SERVICE_VERSION,
            "version": 2,
            "constraints": [
                {"attribute": "slice_thickness_mm", "type": "range", "min": 0.5,
                 "max": 1.5},
            ],
        }
    )
    safety_repo.declare_envelope(safety_db, narrowed, declared_by=OPERATOR)
    safety_db.commit()

    resolved = safety_repo.resolve_envelope(
        safety_db, subject_id=SLICE_SERVICE_ID, subject_version=SLICE_SERVICE_VERSION
    )
    assert resolved is not None
    assert resolved.envelope.version == 2
    assert resolved.envelope_digest == narrowed.digest


def test_an_envelope_decision_is_append_only_and_its_zone_matches_its_outcome(
    safety_db: psycopg.Connection[Any],
) -> None:
    """`MOS-EVID-104`'s record, and the CHECK that keeps it coherent.

    A row saying `zone = OUT, outcome = proceed` would make the monitoring export a
    fiction, so the combination is refused by the database rather than by a convention.
    """
    envelope_row = safety_repo.declare_envelope(
        safety_db, slice_envelope(), declared_by=OPERATOR
    )
    verdict = evaluate(
        slice_envelope(),
        {"slice_thickness_mm": 9.0, "pixel_spacing_mm_max": 0.7, "z_coverage_mm": 300.0,
         "instance_count": 300, "patient_age_years": 64, "manufacturer": "SIEMENS",
         "convolution_kernel_class": "SOFT", "contrast_phase": "NONE",
         "body_part_examined": "CHEST"},
    )
    public_id = safety_repo.record_decision(
        safety_db, verdict, envelope_row=envelope_row,
        study_instance_uid="1.2.3.4", series_instance_uid="1.2.3.4.5", job_uuid=None,
    )
    safety_db.commit()
    assert public_id.startswith("envd_")

    with pytest.raises(psycopg.DatabaseError, match="immutable"):
        safety_db.execute(
            "UPDATE envelope_decisions SET zone = 'IN' WHERE public_id = %s", (public_id,)
        )
    safety_db.rollback()

    with pytest.raises(CheckViolation):
        safety_db.execute(
            """
            INSERT INTO envelope_decisions (
                tenant_id, public_id, study_instance_uid, envelope_id, envelope_version,
                envelope_digest, subject_id, subject_version, zone, marginal_policy,
                outcome)
            VALUES (%s, 'envd_00000000000000000000000000', '1.2.3', %s, 1, %s, 'x', '1',
                    'OUT', 'flag', 'proceed')
            """,
            (DEFAULT_TENANT_ID, envelope_row.id, envelope_row.envelope_digest),
        )
    safety_db.rollback()


def test_the_monitoring_export_groups_by_reason_code(
    safety_db: psycopg.Connection[Any],
) -> None:
    """`MOS-EVID-104`: counts per `(tenant, capability, service_version, reason_code)`.

    "A capability whose envelope rejects a large share of a site's real traffic is a
    procurement fact the site must be able to see on day one, not a silent drop." The
    denormalised `reason_code` column is what makes that an index scan.
    """
    envelope_row = safety_repo.declare_envelope(
        safety_db, slice_envelope(), declared_by=OPERATOR
    )
    base = {"pixel_spacing_mm_max": 0.7, "z_coverage_mm": 300.0, "instance_count": 300,
            "patient_age_years": 64, "manufacturer": "SIEMENS",
            "convolution_kernel_class": "SOFT", "contrast_phase": "NONE",
            "body_part_examined": "CHEST"}
    for thickness, count in ((9.0, 3), (1.0, 2)):
        for _ in range(count):
            safety_repo.record_decision(
                safety_db,
                evaluate(slice_envelope(), {**base, "slice_thickness_mm": thickness}),
                envelope_row=envelope_row,
                study_instance_uid="1.2.3.4",
                series_instance_uid=None,
                job_uuid=None,
                capability_id="lung_segmentation",
            )
    safety_db.commit()

    rows = safety_db.execute(
        "SELECT reason_code, count(*) AS n FROM envelope_decisions "
        "GROUP BY subject_id, subject_version, capability_id, reason_code "
        "ORDER BY reason_code NULLS FIRST"
    ).fetchall()
    counts = {r["reason_code"]: r["n"] for r in rows}
    assert counts[None] == 2
    assert counts["envelope.slice_thickness_out_of_range"] == 3


# =====================================================================================
# 6. The gate in the pipeline -- the REJECTED path 0.1.0 already built
# =====================================================================================
class _Ctx:
    """The three members `_evaluate_declared_envelope` reads off a `StepContext`.

    A stand-in and not a `StepContext`, because building the real one needs a gateway, a
    work root and a capability registry -- none of which this step touches -- and a test
    that had to construct them would be testing the fixture.
    """

    def __init__(self, conn: Any, job: dict[str, Any]) -> None:
        self.conn = conn
        self.job = job

    @property
    def study_instance_uid(self) -> str:
        return str(self.job["study_instance_uid"])


def _queued_job(conn: psycopg.Connection[Any], study_uid: str) -> dict[str, Any]:
    from medos.db.queue import PostgresJobQueue

    created = repo.create_job_queued(
        conn,
        PostgresJobQueue(conn),
        repo.JobSpec(
            study_instance_uid=study_uid,
            capability_ids=("lung_segmentation",),
            service_id=SLICE_SERVICE_ID,
            service_version=SLICE_SERVICE_VERSION,
            created_by_kind="user",
            created_by_id=OPERATOR,
        ),
    )
    job = repo.get_job(conn, created.job_id)
    assert job is not None
    return job


def test_a_study_outside_the_envelope_is_rejected_by_the_worker_step(
    safety_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """The gate, end to end: real envelope row, real job row, real headers, real raise.

    `MOS-DATA-077`: "If a job is **explicitly requested** for a named `ServiceVersion` ...
    a `Job` MUST always be created, and an `applicability` failure terminates it in
    `REJECTED` with reason `outside_applicability_envelope`."

    The decision row is written BEFORE the raise: `MOS-EVID-104` requires the refusal to be
    countable, and a rejection that is not counted is a rejection the site cannot see.
    """
    from medos.worker.steps import PipelineState, _evaluate_declared_envelope

    safety_repo.declare_envelope(safety_db, slice_envelope(), declared_by=OPERATOR)
    study_uid, series_uid, paths = _synthetic_series(
        tmp_path / "thick", n=50, slice_thickness=8.0, series_description="Thorax 8.0 B31f"
    )
    job = _queued_job(safety_db, study_uid)
    safety_db.commit()

    state = PipelineState()
    state.instance_paths = tuple(paths)
    state.series_instance_uid = series_uid

    with pytest.raises(EnvelopeViolation) as exc:
        _evaluate_declared_envelope(_Ctx(safety_db, job), state)
    safety_db.commit()

    assert exc.value.reason_code == "outside_applicability_envelope"
    codes = {v["reason_code"] for v in exc.value.detail["violations"]}
    assert "envelope.slice_thickness_out_of_range" in codes

    decision = safety_db.execute(
        "SELECT zone, outcome, reason_code, job_id FROM envelope_decisions"
    ).fetchone()
    assert decision is not None, (
        "the refusal was not recorded; MOS-EVID-104 makes the rejected share of a site's "
        "traffic a procurement fact it must be able to see"
    )
    assert (decision["zone"], decision["outcome"]) == ("OUT", "rejected")
    assert decision["reason_code"] == "envelope.slice_thickness_out_of_range"
    assert str(decision["job_id"]) == str(job["id"])


def test_an_in_envelope_study_proceeds_and_is_recorded(
    safety_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """The control group. A clean thin recon passes, and the decision is still recorded."""
    from medos.worker.steps import PipelineState, _evaluate_declared_envelope

    safety_repo.declare_envelope(safety_db, slice_envelope(), declared_by=OPERATOR)
    study_uid, series_uid, paths = _synthetic_series(tmp_path / "thin", n=200)
    job = _queued_job(safety_db, study_uid)
    safety_db.commit()

    state = PipelineState()
    state.instance_paths = tuple(paths)
    state.series_instance_uid = series_uid

    detail = _evaluate_declared_envelope(_Ctx(safety_db, job), state)
    safety_db.commit()

    assert detail["declared"] is True
    assert detail["zone"] == "IN"
    assert detail["outcome"] == "proceed"
    assert state.envelope_verdict is not None and state.envelope_verdict.in_envelope


def test_a_marginal_study_proceeds_but_the_provenance_does_not_claim_in_envelope(
    safety_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """`MOS-EVID-101` row 2 plus `MOS-SAFE-085`.

    The pipeline used to write a literal `{"in_envelope": true, "violations": []}` into
    every provenance record. For a flagged study that is a false statement about the
    evidence behind the number the record is attached to, which is the one thing a
    provenance record exists not to make.
    """
    from medos.worker.result_rows import _applicability_record
    from medos.worker.steps import PipelineState, _evaluate_declared_envelope

    safety_repo.declare_envelope(safety_db, slice_envelope(), declared_by=OPERATOR)
    study_uid, series_uid, paths = _synthetic_series(
        tmp_path / "sharp", n=200, kernel="B70f", series_description="Thorax 1.0 B70f"
    )
    job = _queued_job(safety_db, study_uid)
    safety_db.commit()

    state = PipelineState()
    state.instance_paths = tuple(paths)
    state.series_instance_uid = series_uid
    detail = _evaluate_declared_envelope(_Ctx(safety_db, job), state)
    safety_db.commit()

    assert detail["zone"] == "MARGINAL"
    assert detail["outcome"] == "proceed_flagged"

    record = _applicability_record(state)
    assert record["declared"] is True
    assert record["in_envelope"] is False
    assert record["zone"] == "MARGINAL"
    assert record["violations"][0]["attribute"] == "convolution_kernel_class"


def test_no_declared_envelope_is_reported_as_unchecked_not_as_in_envelope(
    safety_db: psycopg.Connection[Any], tmp_path: Path
) -> None:
    """A third state, and it must not collapse into either of the other two.

    "We did not check" is a different fact from "we checked and it was inside", and
    `MOS-EVID-095`'s consequence for the gap belongs to `MOS-SAFE-036`'s clinical-promotion
    gate, not to this step.
    """
    from medos.worker.result_rows import _applicability_record
    from medos.worker.steps import PipelineState, _evaluate_declared_envelope

    study_uid, series_uid, paths = _synthetic_series(tmp_path / "noenv", n=20)
    job = _queued_job(safety_db, study_uid)
    safety_db.commit()

    state = PipelineState()
    state.instance_paths = tuple(paths)
    state.series_instance_uid = series_uid

    detail = _evaluate_declared_envelope(_Ctx(safety_db, job), state)
    assert detail["declared"] is False
    assert "MOS-EVID-095" in detail["consequence"]
    assert state.envelope_verdict is None

    record = _applicability_record(state)
    assert record["in_envelope"] is None, (
        "an unchecked study must not be recorded as in-envelope; null and false are "
        "different facts"
    )


# =====================================================================================
# 7. RUO marking -- the 0.2.0 gate's `ruo-marking`
# =====================================================================================
class _Plan:
    """The one member `apply_ai_marking` reads off an `OutputPlan`."""

    provenance_record_uid = "1.2.826.0.1.3680043.10.1.99.1"


class _Identity:
    """A `JobIdentity` stand-in with the fields `apply_ai_marking` reads.

    The REAL `apply_ai_marking` and the REAL `ai_series_description` are used below, so
    this test exercises the writer's own marking code rather than an imitation of it. The
    identity is a stand-in only because `build_job_identity` would also derive an
    idempotency key, which this test has no opinion about.
    """

    def __init__(self, mode: str) -> None:
        self.clinical_use_mode = mode
        self.service_id = SLICE_SERVICE_ID
        self.service_version = SLICE_SERVICE_VERSION
        self.medicalos_version = "0.2.0"
        self.deployment_id = DEPLOYMENT_ID
        self.job_id = "job_01J000000000000000000000"
        self.preprocessing_spec_digest = "sha256:" + "0" * 64
        self.uid_space = "source"


def _marked_seg(mode: str) -> Dataset:
    ds = Dataset()
    # `apply_ai_marking` derives the DimensionOrganizationUID from the SOPInstanceUID
    # (MOS-IMG-062), so the stub carries one; the value is irrelevant to the marking gate.
    ds.SOPInstanceUID = "1.2.826.0.1.3680043.10.1.99.10"
    ds.SeriesDescription = ai_series_description("Organ-at-risk segmentation", mode)
    ds.Manufacturer = "MedicalOS (unregistered development build)"
    ds.ImageType = ["DERIVED", "PRIMARY"]
    ds.ContentLabel = "MEDICALOS_AI"
    ds.ContentDescription = (
        "RESEARCH USE ONLY - AI-derived organ-at-risk segmentation"
        if mode == "RESEARCH_ONLY"
        else "AI-derived organ-at-risk segmentation"
    )
    segment = Dataset()
    segment.SegmentAlgorithmType = "AUTOMATIC"
    ds.SegmentSequence = [segment]
    apply_ai_marking(ds, _Identity(mode), _Plan(), is_seg=True, src=_SOURCE)
    return ds


#: A source instance for `apply_ai_marking`'s de-identification guard. These two helpers
#: build objects in the SOURCE uid space (`_Identity.uid_space` is "source"), which the
#: guard returns from immediately, so its content does not matter here -- but the
#: parameter is required, because a guard a caller may omit is one a caller will omit.
_SOURCE = Dataset()


def _marked_sr(mode: str) -> Dataset:
    ds = Dataset()
    ds.SeriesDescription = ai_series_description("Organ-at-risk measurements", mode)
    ds.Manufacturer = "MedicalOS (unregistered development build)"
    ds.VerificationFlag = "UNVERIFIED"
    observer = Dataset()
    observer.ValueType = "UIDREF"
    concept = Dataset()
    concept.CodeValue = "121012"
    concept.CodingSchemeDesignator = "DCM"
    concept.CodeMeaning = "Device Observer UID"
    observer.ConceptNameCodeSequence = [concept]
    observer.UID = "1.2.826.0.1.3680043.10.1.99.2"
    ds.ContentSequence = [observer]
    apply_ai_marking(ds, _Identity(mode), _Plan(), is_seg=False, src=_SOURCE)
    apply_research_marking(ds, object_kind="SR", mode=mode)
    return ds


def test_a_fully_marked_object_passes_in_both_modes() -> None:
    """The control group: the writer's own marks satisfy the writer's own gate."""
    for mode in ("RESEARCH_ONLY", "CLINICAL"):
        assert marking_violations(_marked_seg(mode), object_kind="SEG", mode=mode) == ()
        assert marking_violations(_marked_sr(mode), object_kind="SR", mode=mode) == ()


@pytest.mark.parametrize(
    ("attribute", "expected_rule"),
    [
        ("SeriesDescription", "MOS-IMG-136"),
        ("Manufacturer", "MOS-IMG-133"),
        ("ImageType", "MOS-IMG-133"),
        ("ContentDescription", "MOS-IMG-136"),
        ("ContributingEquipmentSequence", "MOS-IMG-134"),
    ],
)
def test_removing_any_seg_marker_makes_the_writer_refuse(
    attribute: str, expected_rule: str
) -> None:
    """`MOS-SAFE-039`: "MUST fail the Job rather than emit an unmarked object".

    One removal at a time, because the defect this is guarding against is a SINGLE mark
    going missing -- a truncation, a copied-over attribute, a library that dropped a field
    it did not recognise -- and a check that only catches a wholly unmarked object catches
    none of those.
    """
    seg = _marked_seg("RESEARCH_ONLY")
    delattr(seg, attribute)
    with pytest.raises(MarkingAbsent) as exc:
        assert_marked(seg, object_kind="SEG", mode="research_only")
    assert exc.value.reason_code == "safety_marking_absent"
    assert expected_rule in {v.rule for v in exc.value.violations}


def test_an_sr_without_the_research_banner_is_refused() -> None:
    """`MOS-IMG-136`'s SR row, which was simply absent before this release.

    The SEG carried its `RESEARCH USE ONLY - ` ContentDescription and the private
    ClinicalUseMode was written, but the SR had no research content item at all -- so an SR
    opened in a viewer that renders the document tree showed no research marking whatever.
    """
    sr = _marked_sr("RESEARCH_ONLY")
    sr.ContentSequence = [
        item for item in sr.ContentSequence
        if "RESEARCH USE ONLY" not in str(getattr(item, "TextValue", ""))
    ]
    with pytest.raises(MarkingAbsent, match="ContentSequence"):
        assert_marked(sr, object_kind="SR", mode="research_only")


def test_the_private_clinical_use_mode_must_match_the_job(  ) -> None:
    """`MOS-SAFE-044`: two Deployments of one ServiceVersion must be distinguishable "by
    the DICOM marker set alone, without consulting the database".

    An object built in clinical mode and checked as research (or the reverse) is exactly
    the confusion that requirement forbids, and both directions must fail.
    """
    research = _marked_seg("RESEARCH_ONLY")
    with pytest.raises(MarkingAbsent):
        assert_marked(research, object_kind="SEG", mode="clinical")

    clinical = _marked_seg("CLINICAL")
    with pytest.raises(MarkingAbsent):
        assert_marked(clinical, object_kind="SEG", mode="research_only")


def test_a_research_sr_may_not_carry_a_verifying_observer() -> None:
    """`MOS-SAFE-042` (E6): `VerificationFlag` forced UNVERIFIED and
    `VerifyingObserverSequence` absent. "No `ResultReview` outcome may change this"."""
    sr = _marked_sr("RESEARCH_ONLY")
    observer = Dataset()
    observer.VerifyingObserverName = "SOMEONE^A"
    sr.VerifyingObserverSequence = [observer]
    sr.VerificationFlag = "VERIFIED"
    with pytest.raises(MarkingAbsent) as exc:
        assert_marked(sr, object_kind="SR", mode="research_only")
    rules = {v.rule for v in exc.value.violations}
    assert rules == {"MOS-SAFE-042"}


def test_the_marking_check_has_no_off_switch() -> None:
    """`MOS-SAFE-039`: "There is no configuration flag that disables it."

    A requirement about the SIGNATURE as much as about the body. A flag that exists is a
    flag that is set to False during an incident at 02:00 and never set back.
    """
    from medos.safety import marking

    signature = inspect.signature(marking.assert_marked)
    assert set(signature.parameters) == {"obj", "object_kind", "mode"}, (
        f"assert_marked grew a parameter: {sorted(signature.parameters)}"
    )
    # Code only: the module's own docstring names the flags it refuses to have, and a
    # scan that tripped on that sentence would punish the file for explaining itself.
    source = _executable_source(Path(marking.__file__))
    for forbidden in ("strict=", "warn_only", "os.environ", "getenv", "MEDOS_SKIP"):
        assert forbidden not in source, (
            f"{forbidden!r} appears in medos/medos/safety/marking.py; MOS-SAFE-039 admits no "
            "way to disable the check"
        )


def test_ruo_marking() -> None:
    """The integration-suite twin of the 0.2.0 gate check. docs/spec/15-delivery.md 15.1.2:

        `ruo-marking` (in `clinical_use_mode: research_only` the writer refuses to emit an
        unmarked object)

    THE GATE CHECK ITSELF IS `tests/gate/test_ruo_marking.py`, and it is marked
    `gate_0_2_0`. This one is NOT marked, deliberately: `tests/gate/conftest.py` resolves a
    marked item to a check name by its MODULE, so a marked test outside `tests/gate/` would
    be selected by `pytest -m gate_0_2_0` and counted by neither the completeness guard nor
    the per-check report. `tests/unit/test_gate_contract.py` asserts that no gate marker
    appears outside `tests/gate/`.

    It stays here because it asserts the same property one layer down -- over
    `medos.safety.marking` directly, with the stub objects this module already builds --
    and the gate's version drives the real `build_seg`/`build_sr`. Two witnesses at two
    layers is the point; one of them being the release gate is what makes it binding.

    The check is stated in the gate's own terms -- REFUSES TO EMIT -- so it asserts the
    negative: a research-mode object with a marker removed must raise, and the writer must
    be the thing that raises. The positive half (a correctly marked object passes) is
    asserted first, because a gate that only ever sees failures cannot tell "the check
    works" from "the check refuses everything".
    """
    seg = _marked_seg("RESEARCH_ONLY")
    sr = _marked_sr("RESEARCH_ONLY")
    assert_marked(seg, object_kind="SEG", mode="research_only")
    assert_marked(sr, object_kind="SR", mode="research_only")

    for obj, kind, removal in (
        (_marked_seg("RESEARCH_ONLY"), "SEG", "SeriesDescription"),
        (_marked_seg("RESEARCH_ONLY"), "SEG", "ContentDescription"),
        (_marked_seg("RESEARCH_ONLY"), "SEG", "ContributingEquipmentSequence"),
        (_marked_sr("RESEARCH_ONLY"), "SR", "ContentSequence"),
    ):
        delattr(obj, removal)
        with pytest.raises(MarkingAbsent) as exc:
            assert_marked(obj, object_kind=kind, mode="research_only")  # type: ignore[arg-type]
        assert exc.value.reason_code == "safety_marking_absent", (
            "MOS-SAFE-035 E3 fixes the code: 'object not emitted; Job FAILED, "
            "error.code = safety_marking_absent'"
        )

    # And the gate is wired into the writers, not merely available to them: both call the
    # shared function, and the pre-STOW call runs before the POST.
    test_the_writers_share_one_marking_function()


def test_the_writers_share_one_marking_function() -> None:
    """`MOS-SAFE-039`: "MUST be implemented as a single function shared by the SEG, SR and
    SC writers", and it must run "immediately before STOW-RS".

    Asserted over the source because the property is about WHERE the call is, and a runtime
    assertion would only prove that one path happened to call it.
    """
    for module in ("medos/medos/writer/seg.py", "medos/medos/writer/sr.py"):
        text = Path(module).read_text(encoding="utf-8")
        assert "from medos.safety.marking import" in text, f"{module} rolls its own check"
        assert "assert_marked(" in text, f"{module} does not run the marking gate"

    steps = Path("medos/medos/worker/steps.py").read_text(encoding="utf-8")
    store = steps.split("def step_store_dicom")[1].split("\ndef ")[0]
    gate = store.index("assert_marked(")
    stow = store.index("gateway.store_files(")
    assert gate < stow, (
        "the marking gate runs after the STOW; MOS-SAFE-039 requires it on the assembled "
        "dataset immediately BEFORE STOW-RS"
    )


# =====================================================================================
# 8. ResultReview -- MOS-SAFE-058..070
# =====================================================================================
def _result_row(conn: psycopg.Connection[Any], tmp_study: str) -> tuple[str, str]:
    """One `jobs` row and one `results` row to hang reviews off."""
    job = _queued_job(conn, tmp_study)
    row = conn.execute(
        """
        INSERT INTO results (
            tenant_id, job_id, capability_id, capability_version, result_kind,
            clinical_use_mode, study_instance_uid, input_series_uids,
            input_instance_uids, input_instance_count, input_uid_digest,
            preprocessing_version, worker_version, runtime_version)
        VALUES (%s, %s, 'lung_segmentation', '1.0.0', 'segmentation',
                'research_only', %s, ARRAY['1.2.3.4'], ARRAY['1.2.3.4.5'], 1, %s,
                'p/1', 'w/1', 'r/1')
        RETURNING id
        """,
        (DEFAULT_TENANT_ID, job["id"], tmp_study, "a" * 64),
    ).fetchone()
    assert row is not None
    return str(job["public_id"]), str(row["id"])


def test_the_state_machine_is_exactly_MOS_SAFE_059s_table() -> None:
    """Eight rows, comparable by eye to the requirement. Nothing else is reachable."""
    assert set(TRANSITIONS) == {
        (None, "create"),
        ("PENDING", "claim"),
        ("PENDING", "expire"),
        ("PENDING", "supersede"),
        ("IN_REVIEW", "submit"),
        ("IN_REVIEW", "release"),
        ("IN_REVIEW", "supersede"),
        ("ACCEPTED", "reopen"),
        ("MODIFIED", "reopen"),
        ("REJECTED", "reopen"),
        ("EXPIRED", "reopen"),
    }
    assert TERMINAL_STATES == {
        "ACCEPTED", "MODIFIED", "REJECTED", "EXPIRED", "SUPERSEDED"
    }
    # SUPERSEDED is terminal and carries no reopen: a superseded result's review is not
    # corrected, it is replaced along with the result.
    with pytest.raises(InvalidTransition):
        next_state("SUPERSEDED", "reopen")
    with pytest.raises(InvalidTransition):
        next_state("ACCEPTED", "claim")
    assert next_state("IN_REVIEW", "submit", outcome="MODIFIED") == "MODIFIED"


def test_the_permissions_and_events_are_the_normative_names() -> None:
    """`MOS-SAFE-067` and `MOS-SAFE-070`, verbatim, and grammar-valid so a credential can
    actually carry them (`MOS-SEC-031`)."""
    from medos.security.scopes import PERMISSION_RE

    assert set(PERMISSIONS) == {
        "result.review.read",
        "result.review.submit",
        "result.review.assign",
        "result.review.reopen",
        "result.read.research",
        "deployment.approve_clinical",
    }
    for permission in PERMISSIONS:
        assert PERMISSION_RE.match(permission), permission

    assert EVENT_NAMES == {
        "result.review.created",
        "result.review.claimed",
        "result.review.released",
        "result.review.submitted",
        "result.review.expired",
        "result.review.reopened",
        "result.review.superseded",
    }


def test_a_full_review_cycle_and_the_projection_that_follows_it(
    safety_db: psycopg.Connection[Any]
) -> None:
    """Open, claim, submit, reopen -- and `results.review_status` tracks the highest round.

    `MOS-SAFE-062` makes the projection a trigger-maintained column "in the same
    transaction as the review transition", and `MOS-SAFE-064` makes rendering it mandatory:
    "The reviewer's disagreement is data, not an erasure."
    """
    _job_id, result_id = _result_row(safety_db, "1.2.3.10")
    policy = DeploymentSafetyPolicy(
        deployment_id=DEPLOYMENT_ID, review_mode="mandatory_post_publication",
        review_sla_hours=48,
    )

    review = safety_repo.open_review(safety_db, result_id=result_id, policy=policy)
    assert review is not None
    assert review.public_id.startswith("rrv_")
    assert review.state == "PENDING"
    assert review.review_due_at is not None
    safety_db.commit()

    status = safety_db.execute(
        "SELECT review_status FROM results WHERE id = %s", (result_id,)
    ).fetchone()
    assert status is not None and status["review_status"] == "PENDING"

    claimed = safety_repo.claim_review(
        safety_db, review.public_id, reviewer_user_id=OPERATOR
    )
    assert claimed.state == "IN_REVIEW"
    assert claimed.claimed_at is not None

    submitted = safety_repo.submit_review(
        safety_db,
        review.public_id,
        outcome="REJECTED",
        reviewer_user_id=OPERATOR,
        reviewer_role="clinical_reviewer",
        reviewer_class="non_clinical",
        action_rationale=RATIONALE,
        rejection_reason="mis_segmentation",
    )
    safety_db.commit()
    assert submitted.state == "REJECTED"
    assert submitted.submitted_at is not None

    status = safety_db.execute(
        "SELECT review_status FROM results WHERE id = %s", (result_id,)
    ).fetchone()
    assert status is not None and status["review_status"] == "REJECTED"

    # MOS-SAFE-066: a new ROW at round + 1. The previous round is untouched.
    round_two = safety_repo.reopen_review(
        safety_db, review.public_id, action_rationale=RATIONALE, policy=policy
    )
    safety_db.commit()
    assert round_two.round == 2
    assert round_two.state == "PENDING"
    assert round_two.public_id != review.public_id

    original = safety_repo.get_review(safety_db, review.public_id)
    assert original.state == "REJECTED", "reopening edited the round it reopened"

    rounds = safety_repo.get_review_rounds(safety_db, review.public_id)
    assert [r.round for r in rounds] == [1, 2]


def test_a_submitted_review_cannot_be_edited_or_deleted(
    safety_db: psycopg.Connection[Any]
) -> None:
    """`MOS-SAFE-060`, in the database. "a review is corrected by a new round, never by
    mutating a submitted row" (`MOS-SAFE-059`).

    Through SQL and not through the repository, because a rule enforced only in application
    code is one forgotten path away from nothing.
    """
    _job_id, result_id = _result_row(safety_db, "1.2.3.11")
    policy = DeploymentSafetyPolicy(deployment_id=DEPLOYMENT_ID, review_mode="optional")
    review = safety_repo.open_review(safety_db, result_id=result_id, policy=policy)
    assert review is not None
    safety_repo.claim_review(safety_db, review.public_id, reviewer_user_id=OPERATOR)
    safety_repo.submit_review(
        safety_db, review.public_id, outcome="ACCEPTED", reviewer_user_id=OPERATOR,
        reviewer_role="clinical_reviewer", reviewer_class="non_clinical",
    )
    safety_db.commit()

    with pytest.raises(psycopg.DatabaseError, match="immutable"):
        safety_db.execute(
            "UPDATE result_reviews SET state = 'MODIFIED' WHERE public_id = %s",
            (review.public_id,),
        )
    safety_db.rollback()

    with pytest.raises(psycopg.DatabaseError, match="immutable"):
        safety_db.execute(
            "DELETE FROM result_reviews WHERE public_id = %s", (review.public_id,)
        )
    safety_db.rollback()

    # And a PENDING row -- not yet terminal -- still may not be DELETED. A deleted PENDING
    # row destroys the record that a review was ever required.
    _job2, result_two = _result_row(safety_db, "1.2.3.12")
    pending = safety_repo.open_review(safety_db, result_id=result_two, policy=policy)
    assert pending is not None
    safety_db.commit()
    with pytest.raises(psycopg.DatabaseError, match="immutable"):
        safety_db.execute(
            "DELETE FROM result_reviews WHERE public_id = %s", (pending.public_id,)
        )
    safety_db.rollback()


def test_a_review_cannot_be_moved_onto_a_different_result(
    safety_db: psycopg.Connection[Any]
) -> None:
    """`result_id`, `round` and `public_id` are sealed from insert, not merely once
    terminal. Moving a review onto another result would reattribute a human conclusion."""
    _job_id, result_id = _result_row(safety_db, "1.2.3.13")
    _job2, other_id = _result_row(safety_db, "1.2.3.14")
    policy = DeploymentSafetyPolicy(deployment_id=DEPLOYMENT_ID, review_mode="optional")
    review = safety_repo.open_review(safety_db, result_id=result_id, policy=policy)
    assert review is not None
    safety_db.commit()

    with pytest.raises(psycopg.DatabaseError, match="sealed"):
        safety_db.execute(
            "UPDATE result_reviews SET result_id = %s WHERE public_id = %s",
            (other_id, review.public_id),
        )
    safety_db.rollback()


def test_the_conditional_fields_are_required_where_MOS_SAFE_058_says(
    safety_db: psycopg.Connection[Any]
) -> None:
    """`MOS-SAFE-069`: "422 on missing conditional fields", with a pointer per field."""
    with pytest.raises(SubmissionInvalid) as exc:
        validate_submission(
            outcome="MODIFIED", action_rationale="too short",
            modifications=None, rejection_reason=None,
        )
    pointers = {p["pointer"] for p in exc.value.pointers}
    assert pointers == {"/action_rationale", "/modifications"}

    with pytest.raises(SubmissionInvalid) as exc:
        validate_submission(
            outcome="REJECTED", action_rationale=RATIONALE,
            modifications=None, rejection_reason=None,
        )
    assert exc.value.pointers[0]["pointer"] == "/rejection_reason"

    # An ACCEPTED review that carries a patch is a MODIFIED review that was mislabelled.
    with pytest.raises(SubmissionInvalid):
        validate_submission(
            outcome="ACCEPTED", action_rationale=None,
            modifications=[{"op": "replace", "path": "/x", "value": 1}],
            rejection_reason=None,
        )

    # The database says the same thing, so a path that bypassed the validator still fails.
    _job_id, result_id = _result_row(safety_db, "1.2.3.15")
    with pytest.raises(CheckViolation):
        safety_db.execute(
            """
            INSERT INTO result_reviews (
                tenant_id, result_id, public_id, round, state, reviewer_user_id,
                reviewer_class, submitted_at)
            VALUES (%s, %s, 'rrv_00000000000000000000000000', 1, 'REJECTED', %s,
                    'non_clinical', now())
            """,
            (DEFAULT_TENANT_ID, result_id, OPERATOR),
        )
    safety_db.rollback()


def test_a_second_principal_cannot_claim_or_submit(
    safety_db: psycopg.Connection[Any]
) -> None:
    """`MOS-SAFE-069`: "409 `already_claimed` if `reviewer_user_id` set by another
    principal", and release/submit are the claimant's alone."""
    _job_id, result_id = _result_row(safety_db, "1.2.3.16")
    policy = DeploymentSafetyPolicy(deployment_id=DEPLOYMENT_ID, review_mode="optional")
    review = safety_repo.open_review(safety_db, result_id=result_id, policy=policy)
    assert review is not None
    safety_repo.claim_review(safety_db, review.public_id, reviewer_user_id=OPERATOR)
    safety_db.commit()

    other = "22222222-2222-2222-2222-222222222222"
    with pytest.raises(safety_repo.ReviewConflict) as exc:
        safety_repo.claim_review(safety_db, review.public_id, reviewer_user_id=other)
    assert exc.value.code in ("already_claimed", "invalid_transition")
    safety_db.rollback()

    with pytest.raises(safety_repo.ReviewConflict) as exc:
        safety_repo.release_review(safety_db, review.public_id, reviewer_user_id=other)
    assert exc.value.code == "not_claimant"
    safety_db.rollback()


def test_expiry_and_the_claim_lease(safety_db: psycopg.Connection[Any]) -> None:
    """`PENDING` expires on `review_due_at`; `IN_REVIEW` returns to `PENDING` on the
    60-minute lease. The two are different fates on purpose -- expiring a review someone is
    in the middle of would strand the reader."""
    _job_id, result_id = _result_row(safety_db, "1.2.3.17")
    policy = DeploymentSafetyPolicy(
        deployment_id=DEPLOYMENT_ID, review_mode="mandatory_post_publication",
        review_sla_hours=1,
    )
    review = safety_repo.open_review(safety_db, result_id=result_id, policy=policy)
    assert review is not None
    safety_db.commit()

    later = datetime.now(UTC) + timedelta(hours=2)
    expired = safety_repo.expire_due_reviews(safety_db, now=later)
    safety_db.commit()
    assert [r.public_id for r in expired] == [review.public_id]
    assert expired[0].state == "EXPIRED"

    # An EXPIRED row is reopenable (MOS-SAFE-059's last row) and is otherwise terminal.
    reopened = safety_repo.reopen_review(
        safety_db, review.public_id, action_rationale=RATIONALE, policy=policy
    )
    safety_db.commit()
    assert reopened.round == 2

    safety_repo.claim_review(safety_db, reopened.public_id, reviewer_user_id=OPERATOR)
    safety_db.commit()
    released = safety_repo.release_expired_claims(
        safety_db, now=datetime.now(UTC) + timedelta(minutes=61)
    )
    safety_db.commit()
    assert [r.state for r in released] == ["PENDING"]
    assert released[0].reviewer_user_id is None


def test_superseding_a_result_does_not_rewrite_a_completed_review(
    safety_db: psycopg.Connection[Any]
) -> None:
    """A review that was ACCEPTED before the result was superseded stays ACCEPTED.

    It records what a human concluded about the object that existed at the time;
    overwriting it to SUPERSEDED would erase a completed human act.
    """
    _job_id, result_id = _result_row(safety_db, "1.2.3.18")
    policy = DeploymentSafetyPolicy(deployment_id=DEPLOYMENT_ID, review_mode="optional")
    first = safety_repo.open_review(safety_db, result_id=result_id, policy=policy)
    assert first is not None
    safety_repo.claim_review(safety_db, first.public_id, reviewer_user_id=OPERATOR)
    safety_repo.submit_review(
        safety_db, first.public_id, outcome="ACCEPTED", reviewer_user_id=OPERATOR,
        reviewer_role="clinical_reviewer", reviewer_class="non_clinical",
    )
    second = safety_repo.reopen_review(
        safety_db, first.public_id, action_rationale=RATIONALE, policy=policy
    )
    safety_db.commit()

    superseded = safety_repo.supersede_reviews_for_result(safety_db, result_id=result_id)
    safety_db.commit()
    assert [r.public_id for r in superseded] == [second.public_id]
    assert safety_repo.get_review(safety_db, first.public_id).state == "ACCEPTED"


def test_a_review_cycle_enqueues_no_job_and_stows_nothing(
    safety_db: psycopg.Connection[Any]
) -> None:
    """`MOS-SAFE-063` asks chapter 14 for exactly this test.

    "Chapter 14 MUST include a test asserting that a full review cycle produces zero rows
    in `jobs` and zero STOW-RS calls when `emit_verified_sr_on_accept` is false."

    The job row the result hangs off is created BEFORE the cycle and counted first, so what
    is asserted is the delta: the review path added none.
    """
    _job_id, result_id = _result_row(safety_db, "1.2.3.19")
    safety_db.commit()
    jobs_before = safety_db.execute("SELECT count(*) AS n FROM jobs").fetchone()
    objects_before = safety_db.execute(
        "SELECT count(*) AS n FROM result_dicom_objects"
    ).fetchone()
    assert jobs_before is not None and objects_before is not None

    policy = DeploymentSafetyPolicy(deployment_id=DEPLOYMENT_ID, review_mode="optional")
    assert policy.emit_verified_sr_on_accept is False
    review = safety_repo.open_review(safety_db, result_id=result_id, policy=policy)
    assert review is not None
    safety_repo.claim_review(safety_db, review.public_id, reviewer_user_id=OPERATOR)
    safety_repo.submit_review(
        safety_db, review.public_id, outcome="MODIFIED", reviewer_user_id=OPERATOR,
        reviewer_role="clinical_reviewer", reviewer_class="non_clinical",
        action_rationale=RATIONALE,
        modifications=[{"op": "replace", "path": "/findings/0/measurements/1/value",
                        "value": 410}],
    )
    safety_repo.reopen_review(
        safety_db, review.public_id, action_rationale=RATIONALE, policy=policy
    )
    safety_db.commit()

    jobs_after = safety_db.execute("SELECT count(*) AS n FROM jobs").fetchone()
    objects_after = safety_db.execute(
        "SELECT count(*) AS n FROM result_dicom_objects"
    ).fetchone()
    assert jobs_after is not None and objects_after is not None
    assert jobs_after["n"] == jobs_before["n"], (
        "a ResultReview transition enqueued a Job; MOS-SAFE-063's side-effect list is "
        "closed and does not contain one"
    )
    assert objects_after["n"] == objects_before["n"], (
        "a ResultReview transition wrote a DICOM object row"
    )


# =====================================================================================
# 9. reviewer_class, the verified-SR gate, and the open question
# =====================================================================================
def test_reviewer_class_is_non_clinical_without_a_qualified_role() -> None:
    """`MOS-SAFE-068`, evaluated rather than stubbed.

    "`reviewer_class = clinical` if and only if BOTH hold: the submitting principal is a
    `User` ... AND the `Role` named by `reviewer_role` is one the principal currently holds
    and carries `clinically_qualified = true`." This deployment has no `roles` table, so no
    principal holds any role, so the second conjunct is false for everyone.
    """
    assert reviewer_class_for(
        principal_kind="service_account", principal_id="x",
        reviewer_role="clinical_reviewer",
    ) == "non_clinical"
    assert reviewer_class_for(
        principal_kind="user", principal_id="x", reviewer_role="clinical_reviewer",
    ) == "non_clinical"
    assert reviewer_class_for(
        principal_kind="user", principal_id="x", reviewer_role=None,
    ) == "non_clinical"

    class _Qualified:
        def holds_clinically_qualified_role(
            self, *, principal_kind: str, principal_id: str, role_key: str
        ) -> bool:
            return True

    # And the seam works: when chapter 8's roles land, the answer changes for a User only.
    assert reviewer_class_for(
        principal_kind="user", principal_id="x", reviewer_role="anything",
        roles=_Qualified(),
    ) == "clinical"
    assert reviewer_class_for(
        principal_kind="service_account", principal_id="x", reviewer_role="anything",
        roles=_Qualified(),
    ) == "non_clinical", (
        "MOS-SAFE-068: machine review is not human review, under any configuration"
    )


def test_no_role_key_is_hardcoded_anywhere_in_the_review_path() -> None:
    """`MOS-SAFE-104` / `MOS-SEC-038`: the decision MUST NOT come from the role `key`.

    "a value set such as `RADIOLOGIST`/`CLINICIAN`/`RESEARCHER` MUST NOT appear anywhere as
    an enum, a database CHECK or a lookup."
    """
    for path in ("medos/medos/safety/review.py", "medos/medos/safety/repo.py",
                 "medos/medos/api/routes_reviews.py"):
        source = _executable_source(Path(path))
        for token in ("RADIOLOGIST", "CLINICIAN", "RESEARCHER"):
            assert token not in source, f"{token} appears in the CODE of {path}"

    migration = Path("medos/medos/db/migrations/0010_safety.up.sql").read_text(encoding="utf-8")
    assert "reviewer_role text CHECK (reviewer_role IS NULL OR length" in migration, (
        "reviewer_role must carry a length bound and no value enumeration"
    )
    assert "RADIOLOGIST" not in migration


def test_a_service_account_can_never_produce_a_verified_sr() -> None:
    """`MOS-SAFE-065` gated by `MOS-SAFE-068` and `MOS-SAFE-042`. All four conditions."""
    assert may_emit_verified_sr(
        state="ACCEPTED", reviewer_class="clinical", clinical_use_mode="clinical",
        emit_verified_sr_on_accept=True,
    ) is True
    for kwargs in (
        {"reviewer_class": "non_clinical"},
        {"clinical_use_mode": "research_only"},  # MOS-SAFE-042 forbids it
        {"emit_verified_sr_on_accept": False},
        {"state": "REJECTED"},
    ):
        base = {
            "state": "ACCEPTED", "reviewer_class": "clinical",
            "clinical_use_mode": "clinical", "emit_verified_sr_on_accept": True,
        }
        assert may_emit_verified_sr(**{**base, **kwargs}) is False, kwargs


def test_pre_publication_review_gating_is_refused_and_oq01_stays_open() -> None:
    """`MOS-SAFE-061`: `mandatory_pre_publication` MUST be refused in 0.1-0.4, with the
    requirement's own `detail`. Chapter 16 carries the question (OQ-01) and this release
    does not answer it.

    Refusing is the mechanism: a half-built gating path would settle the open question by
    accident, in code, without anyone deciding.
    """
    with pytest.raises(ReviewModeReserved) as exc:
        DeploymentSafetyPolicy(
            deployment_id=DEPLOYMENT_ID, review_mode="mandatory_pre_publication"
        )
    assert "reserved" in str(exc.value)
    assert "Chapter 16" in str(exc.value)

    # And nothing in the review surface gates visibility on a review state.
    for path in ("medos/medos/safety/review.py", "medos/medos/safety/repo.py",
                 "medos/medos/api/routes_reviews.py"):
        source = Path(path).read_text(encoding="utf-8")
        assert "def may_publish" not in source
        assert "def is_visible" not in source


def test_clinical_use_mode_has_no_environment_override() -> None:
    """`MOS-SAFE-033`: "There is no tenant-wide, service-wide or environment-wide
    override."

    `MOS-SAFE-036`'s E1 gate -- nine conditions and a named approver -- is how a Deployment
    becomes clinical. An env var would be that gate's bypass, and it would be set in a
    compose file by someone who wanted the RUO banner to go away.
    """
    from medos.safety import policy as policy_module

    source = _executable_source(Path(policy_module.__file__))
    assert "MEDOS_CLINICAL_USE_MODE" not in source
    assert policy_module.default_policy_source(
        {"MEDOS_CLINICAL_USE_MODE": "clinical"}
    ).policy.clinical_use_mode == "research_only"

    # And 0002's refusal still stands: no tenant-wide default column.
    tenancy = Path("medos/medos/db/migrations/0002_tenancy.up.sql").read_text(encoding="utf-8")
    assert "default_clinical_use_mode" not in tenancy.replace(
        "-- deliberately NO `default_clinical_use_mode` column", ""
    )


def test_review_opens_only_when_the_deployment_asks_for_it(
    safety_db: psycopg.Connection[Any]
) -> None:
    """`MOS-SAFE-059` row 1: "when `Deployment.review_mode != off`". `off` opens nothing."""
    _job_id, result_id = _result_row(safety_db, "1.2.3.20")
    off = DeploymentSafetyPolicy(deployment_id=DEPLOYMENT_ID, review_mode="off")
    assert safety_repo.open_review(safety_db, result_id=result_id, policy=off) is None
    safety_db.commit()

    status = safety_db.execute(
        "SELECT review_status FROM results WHERE id = %s", (result_id,)
    ).fetchone()
    assert status is not None and status["review_status"] == "UNREVIEWED", (
        "MOS-SAFE-062: UNREVIEWED when no row exists"
    )

    # Idempotent: a retry that reaches the result stage again (MOS-STORE-275) must not
    # raise on the unique constraint.
    optional = DeploymentSafetyPolicy(deployment_id=DEPLOYMENT_ID, review_mode="optional")
    first = safety_repo.open_review(safety_db, result_id=result_id, policy=optional)
    second = safety_repo.open_review(safety_db, result_id=result_id, policy=optional)
    safety_db.commit()
    assert first is not None and second is not None
    assert first.public_id == second.public_id


def test_dicom_headers_round_trip_through_pydicom(tmp_path: Path) -> None:
    """A guard on this file's own fixture: if the synthesiser stops producing readable
    Part 10 files, every envelope assertion above would pass vacuously on empty headers."""
    _study, _series, paths = _synthetic_series(tmp_path / "guard", n=3)
    for path in paths:
        ds = pydicom.dcmread(str(path), stop_before_pixels=True)
        assert ds.Modality == "CT"
        assert float(ds.SliceThickness) == 1.0
