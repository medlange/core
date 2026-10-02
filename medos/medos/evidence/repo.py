# SPDX-License-Identifier: Apache-2.0
"""Sealing, freezing, and the row access behind them. Chapter 7, chapter 17 section 17.6.4.

SEALING IS THE VERB THIS MODULE EXISTS FOR.

`MOS-TRAIN-208` gives the procedure and fixes the order:

    1. Freeze the `CurationBatch`; take the `disposition = 'include'` rows.
    2. Apply the `patient_key` alias table of `MOS-TRAIN-116` before anything else.
    3. Retrieve each series through `dataset_export`.
    4. Compute `series_pixel_digest` per `MOS-EVID-018` over stored pixel values.
    5. Build one manifest line per series in the format of chapter 7 section 7.3.2.
    6. Sort by `(patient_key, study_instance_uid, series_instance_uid)`; compute
       `manifest_digest` per `MOS-EVID-009`.
    7. Compute `acquisition_profile` per `MOS-EVID-024`.
    8. Write the manifest object and the `dataset_versions` row in one transaction.

Steps 1-4 belong to the curation and export components: this module's entry point takes
their output as an explicit, materialised sequence of `SeriesRecord`s, which is
`MOS-EVID-016` enforced by the type -- there is no overload taking a query, a folder or a
view, and adding one would make every downstream digest a digest of whatever the database
happened to hold at read time. Steps 5-8 are `seal_dataset_version()` below, plus the
blocking gates the release requires between 7 and 8.

THE BLOCKING GATES AT SEAL, IN THE ORDER THEY RUN

    G1  MOS-EVID-016   the cohort is a non-empty materialised list
    G2  MOS-TRAIN-087  no case at corpus generation 2 or above (enforced in SeriesRecord)
    G3  MOS-TRAIN-118  patient-identity coherence: one study, one accession -> one
                       patient_key. A hit is an IDENTITY defect with a prescribed remedy,
                       and the obvious alternative remedy is explicitly refused.
    G4  MOS-EVID-026   an `acceptance` cohort has >= 30 patients, or no criterion in 7.8
                       can return anything but INDETERMINATE
    G5  MOS-EVID-012   a version that is not `public_deidentified` cannot be shared
    G6  MOS-EVID-021   a version that is not `identified` names its deid policy and its
                       UID mapping table
    G7  MOS-TRAIN-088  the corpus stratification check. C1, C2, C3, C5 and C7 block.

Every one of them refuses with a `Refusal` carrying a stable `check_id` and a machine
-readable `code`. `MOS-TRAIN-088` says "A `fail` MUST block sealing", and a warning that
the caller may ignore is not a block.

WHAT THIS MODULE DOES NOT DO

It does not compute `patient_key`, `accession_number_hash` or `institution_key`. Those
need the per-tenant salt from the chapter 8 tenant keyring, which does not exist yet;
`medos.evidence.digest` holds the three constructions and the caller supplies the salt.
REPORTED as a dependency, not worked around with a hardcoded salt -- a salt in the
repository is not a salt.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from medos.db.tenancy import tenant_tx
from medos.evidence import digest as _digest
from medos.evidence import leakage as _leak
from medos.evidence import stratification as _strat
from medos.evidence.manifest import (
    Acquisition,
    SeriesRecord,
    annotation_manifest_lines,
    dataset_manifest_lines,
    split_manifest_lines,
)
from medos.evidence.profile import KERNEL_CLASS_MAP_VERSION, acquisition_profile
from medos.evidence.store import ManifestStore, manifest_key
from medos.sdk.refusal import FreezeRefused, Refusal, SealRefused

__all__ = [
    "ACCEPTANCE_MIN_PATIENTS",
    "DatasetRow",
    "SealResult",
    "SplitResult",
    "AnnotationSetResult",
    "create_dataset",
    "get_dataset",
    "seal_dataset_version",
    "mark_defective",
    "load_records",
    "freeze_split",
    "freeze_annotation_set",
    "verify_manifest",
]

# MOS-EVID-026: "A DatasetVersion whose `Dataset.purpose` is `acceptance` MUST have
# `n_patients >= 30`. Below that, the platform MUST refuse to seal it for that purpose."
# MOS-TRAIN-114 sets the same floor on a split's `test` partition, for the same reason.
ACCEPTANCE_MIN_PATIENTS = 30


def _row(r: Any) -> dict[str, Any]:
    """Accept a `dict_row` or a tuple row without caring which the caller configured."""
    if r is None:
        raise LookupError("expected a row, got none")
    return dict(r) if isinstance(r, Mapping) else r


# =====================================================================================
# datasets
# =====================================================================================
@dataclass(frozen=True)
class DatasetRow:
    id: str
    public_id: str
    slug: str
    purpose: str
    custodian: str
    visibility: str
    licence_spdx: str | None
    licence_text: str | None


def create_dataset(
    conn: psycopg.Connection[Any],
    *,
    slug: str,
    display_name: str,
    purpose: str,
    custodian: str,
    created_by: str,
    visibility: str = "tenant_private",
    licence_spdx: str | None = None,
    licence_text: str | None = None,
    licence_url: str | None = None,
) -> DatasetRow:
    """One `Dataset` = ONE SOURCE = one licence. Chapter 7 section 7.3.1.

    `custodian` is chapter 7's `source_id` -- `TCIA/LIDC-IDRI`, `site-a/pacs`. It is
    carried verbatim into every manifest and every exported bundle and is the `issuer`
    fallback of `MOS-EVID-010`, so two collections MUST NOT share one row: they would
    share a patient-key space they do not share in reality.

    That one-source-one-licence shape is the reason the release note's "licence per
    source" needs no extra column, and also its limit: a cohort drawn across a CC BY 3.0
    collection and a CC BY-NC 3.0 collection has no single `dataset_id` to hang a licence
    on, because a `DatasetVersion` belongs to exactly one `Dataset`. Chapter 7 provides no
    mechanism for that cohort. REPORTED rather than patched here with a table no chapter
    owns; until it is resolved, such a corpus is two datasets and two versions, and the
    more restrictive term governs anything trained across both.
    """
    if visibility != "tenant_private" and not (licence_spdx or licence_text):
        # The database CHECK says the same thing. Raised here too because the message
        # matters: MOS-EVID-027 is about a licence travelling with an exported bundle,
        # and a bare 23514 does not say that.
        raise ValueError(
            "MOS-EVID-027: licence_spdx or licence_text MUST be non-null for a dataset "
            "whose visibility is not tenant_private, and the licence MUST be carried "
            "into any exported report bundle"
        )
    public_id = _digest.new_dataset_id()
    with tenant_tx(conn):
        row = _row(
            conn.execute(
                """
                INSERT INTO datasets (public_id, tenant_id, slug, display_name, purpose,
                                      custodian, visibility, licence_spdx, licence_text,
                                      licence_url, created_by)
                VALUES (%s, current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, public_id, slug, purpose, custodian, visibility,
                          licence_spdx, licence_text
                """,
                (public_id, slug, display_name, purpose, custodian, visibility,
                 licence_spdx, licence_text, licence_url, created_by),
            ).fetchone()
        )
    return DatasetRow(
        id=str(row["id"]), public_id=row["public_id"], slug=row["slug"],
        purpose=row["purpose"], custodian=row["custodian"],
        visibility=row["visibility"], licence_spdx=row["licence_spdx"],
        licence_text=row["licence_text"],
    )


def get_dataset(conn: psycopg.Connection[Any], dataset_id: str) -> DatasetRow:
    with tenant_tx(conn):
        row = _row(
            conn.execute(
                "SELECT id, public_id, slug, purpose, custodian, visibility, "
                "licence_spdx, licence_text FROM datasets WHERE id = %s",
                (dataset_id,),
            ).fetchone()
        )
    return DatasetRow(
        id=str(row["id"]), public_id=row["public_id"], slug=row["slug"],
        purpose=row["purpose"], custodian=row["custodian"],
        visibility=row["visibility"], licence_spdx=row["licence_spdx"],
        licence_text=row["licence_text"],
    )


# =====================================================================================
# dataset_versions -- THE SEAL
# =====================================================================================
@dataclass(frozen=True)
class SealResult:
    """What a seal returns. `reused` is `MOS-TRAIN-209`'s idempotence outcome."""

    id: str
    public_id: str
    version: int
    manifest_digest: str
    manifest_bucket: str
    manifest_object_key: str
    case_count: int
    patient_count: int
    acquisition_profile: dict[str, Any]
    stratification: dict[str, Any]
    reused: bool = False
    gates: dict[str, str] = field(default_factory=dict)


def seal_dataset_version(
    conn: psycopg.Connection[Any],
    *,
    dataset_id: str,
    records: Sequence[SeriesRecord],
    store: ManifestStore,
    bucket: str,
    sealed_by: str,
    deidentification_status: str,
    deid_policy_id: str | None = None,
    uid_mapping_table_id: str | None = None,
    source_description: str = "",
    parent_version_id: str | None = None,
    derivation: Mapping[str, Any] | None = None,
    evidence_kind: str = "vendor_evidence",
    envelope: Mapping[str, Any] | None = None,
    stratification_waivers: Sequence[Mapping[str, Any]] = (),
    kernel_class_map_version: int = KERNEL_CLASS_MAP_VERSION,
) -> SealResult:
    """Steps 5-8 of `MOS-TRAIN-208`, with the blocking gates between 7 and 8.

    Raises `SealRefused` -- never a warning, never a partially written version -- when any
    blocking gate fails. `MOS-TRAIN-208`: "A failure at any step MUST leave no
    `dataset_versions` row and no manifest object."

    `evidence_kind` defaults to `vendor_evidence`, the STRICT side of C7. A default of
    `site_acceptance` would make the single-site check vacuous for every caller who did
    not think about it, which is the population the check is written for.
    """
    ds = get_dataset(conn, dataset_id)
    refusals: list[Refusal] = []
    gates: dict[str, str] = {}

    # ---- G1  MOS-EVID-016 -----------------------------------------------------------
    if not records:
        raise SealRefused(
            (
                Refusal(
                    check_id="MOS-EVID-016",
                    code="empty_cohort",
                    message="only an explicit, materialised list of instances is a "
                            "DatasetVersion; this one is empty",
                ),
            )
        )
    gates["MOS-EVID-016"] = "pass"

    # ---- G3  MOS-TRAIN-118 ----------------------------------------------------------
    # G2 (MOS-TRAIN-087, corpus generation) is enforced by SeriesRecord.__post_init__, so
    # a generation-2 case cannot reach this function at all.
    gates["MOS-TRAIN-087"] = "pass"
    identity = _leak.patient_identity_report(records)
    for r in _leak.blocking_refusals(identity):
        refusals.append(
            Refusal(
                check_id=r.check_id,
                code="patient_identity_defect",
                message=(
                    f"MOS-TRAIN-118 {r.check_id}: the same "
                    f"{'study' if r.check_id == 'L2' else 'accession'} appears under two "
                    "patient_keys, which is a defect in PATIENT IDENTITY and not a "
                    "partition defect"
                ),
                observed=r.observed,
                bound=0,
                detail=r.detail,
            )
        )
    gates["MOS-TRAIN-118"] = "fail" if identity.failed else "pass"

    # ---- steps 6 and 7 --------------------------------------------------------------
    lines = dataset_manifest_lines(records)
    manifest_digest = _digest.manifest_digest(lines)
    profile = acquisition_profile(
        records, kernel_class_map_version=kernel_class_map_version
    )

    # ---- G4  MOS-EVID-026 -----------------------------------------------------------
    n_patients = int(profile["n_patients"])
    if ds.purpose == "acceptance" and n_patients < ACCEPTANCE_MIN_PATIENTS:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-026",
                code="acceptance_cohort_below_floor",
                message=(
                    f"an `acceptance` cohort MUST have n_patients >= "
                    f"{ACCEPTANCE_MIN_PATIENTS}; below that no criterion in 7.8 can "
                    f"return anything but INDETERMINATE"
                ),
                observed=n_patients,
                bound=ACCEPTANCE_MIN_PATIENTS,
                detail={"purpose": ds.purpose},
            )
        )
    gates["MOS-EVID-026"] = (
        "n/a" if ds.purpose != "acceptance"
        else "pass" if n_patients >= ACCEPTANCE_MIN_PATIENTS else "fail"
    )

    # ---- G5  MOS-EVID-012 -----------------------------------------------------------
    # The cross-table half that no CHECK can see: `visibility` is on `datasets`,
    # `deidentification_status` on `dataset_versions`.
    if ds.visibility != "tenant_private" and deidentification_status != "public_deidentified":
        refusals.append(
            Refusal(
                check_id="MOS-EVID-012",
                code="shared_but_not_public_deidentified",
                message=(
                    "a DatasetVersion whose deidentification_status is not "
                    "`public_deidentified` MUST NOT be set to any visibility other than "
                    "`tenant_private`"
                ),
                observed={"visibility": ds.visibility,
                          "deidentification_status": deidentification_status},
                bound={"visibility": "tenant_private"},
            )
        )
        gates["MOS-EVID-012"] = "fail"
    else:
        gates["MOS-EVID-012"] = "pass"

    # ---- G6  MOS-EVID-021 -----------------------------------------------------------
    if deidentification_status != "identified":
        missing = [
            name
            for name, value in (("deid_policy_id", deid_policy_id),
                                ("uid_mapping_table_id", uid_mapping_table_id))
            if not value
        ]
        if missing:
            refusals.append(
                Refusal(
                    check_id="MOS-EVID-021",
                    code="deidentification_provenance_incomplete",
                    message=(
                        "a pseudonymised or public_deidentified version MUST name its "
                        f"de-identification policy version and its tenant UID mapping "
                        f"table; missing {missing}. A version sealed under one UID "
                        "mapping is not interchangeable with the same images under "
                        "another"
                    ),
                    observed=missing,
                    bound=["deid_policy_id", "uid_mapping_table_id"],
                )
            )
        gates["MOS-EVID-021"] = "fail" if missing else "pass"
    else:
        gates["MOS-EVID-021"] = "n/a"

    # ---- G7  MOS-TRAIN-088 ----------------------------------------------------------
    report = _strat.stratification_report(
        profile,
        evidence_kind=evidence_kind,
        envelope=envelope,
        waivers=stratification_waivers,
    )
    # MOS-TRAIN-088 binds the check to a `training`-purpose seal and to any
    # `vendor_evidence` claim. It is COMPUTED for every seal regardless, because
    # MOS-TRAIN-011 makes the report part of the evidence set a promotion decision must
    # show and recomputing it later from `acquisition_profile` (MOS-TRAIN-093) is only
    # possible if the profile is right, which is the same computation.
    blocking = ds.purpose == "training" or evidence_kind == "vendor_evidence"
    if blocking:
        refusals.extend(_strat.blocking_refusals(report))
    gates["MOS-TRAIN-088"] = (
        ("fail" if report.failed else "pass") if blocking
        else ("advisory_fail" if report.failed else "pass")
    )

    if refusals:
        raise SealRefused(tuple(refusals))

    # ---- MOS-TRAIN-209: idempotent on content ---------------------------------------
    with tenant_tx(conn):
        existing = conn.execute(
            "SELECT id, public_id, version, manifest_bucket, manifest_object_key, "
            "case_count, patient_count, acquisition_profile "
            "FROM dataset_versions WHERE manifest_digest = %s",
            (manifest_digest,),
        ).fetchone()
    if existing is not None:
        e = _row(existing)
        return SealResult(
            id=str(e["id"]), public_id=e["public_id"], version=int(e["version"]),
            manifest_digest=manifest_digest, manifest_bucket=e["manifest_bucket"],
            manifest_object_key=e["manifest_object_key"],
            case_count=int(e["case_count"]), patient_count=int(e["patient_count"]),
            acquisition_profile=dict(e["acquisition_profile"]),
            stratification=report.as_dict(), reused=True, gates=gates,
        )

    # ---- step 8 ---------------------------------------------------------------------
    key = manifest_key(_current_tenant(conn), "datasets", manifest_digest)
    body = _digest.manifest_bytes(lines)
    store.put(bucket, key, body)

    public_id = _digest.new_dataset_version_id()
    studies = {r.study_instance_uid for r in records}
    try:
        with tenant_tx(conn):
            version = int(
                _row(
                    conn.execute(
                        "SELECT coalesce(max(version), 0) + 1 AS v FROM dataset_versions "
                        "WHERE dataset_id = %s",
                        (dataset_id,),
                    ).fetchone()
                )["v"]
            )
            row = _row(
                conn.execute(
                    """
                    INSERT INTO dataset_versions (
                      public_id, tenant_id, dataset_id, version, parent_version_id,
                      derivation, manifest_bucket, manifest_object_key, manifest_digest,
                      manifest_line_count, case_count, patient_count, study_count,
                      series_count, instance_count, source_description,
                      acquisition_profile, deidentification_status, deid_policy_id,
                      uid_mapping_table_id, sealed_by)
                    VALUES (%s, current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, version
                    """,
                    (
                        public_id, dataset_id, version, parent_version_id,
                        Jsonb(dict(derivation)) if derivation is not None else None,
                        bucket, key, manifest_digest, len(lines), len(studies),
                        n_patients, len(studies), len(records),
                        sum(r.instance_count for r in records),
                        source_description or ds.custodian, Jsonb(profile),
                        deidentification_status, deid_policy_id, uid_mapping_table_id,
                        sealed_by,
                    ),
                ).fetchone()
            )
            version_id = str(row["id"])
            conn.cursor().executemany(
                """
                INSERT INTO dataset_cases (
                  tenant_id, dataset_version_id, case_key, series_instance_uid,
                  patient_key, study_instance_uid, modality, sop_class_uid,
                  instance_count, sop_instance_uids, series_pixel_digest,
                  lossy_compressed, accession_number_hash, acquisition, corpus_generation)
                VALUES (current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s)
                """,
                [
                    (
                        version_id, r.case_key, r.series_instance_uid, r.patient_key,
                        r.study_instance_uid, r.modality, r.sop_class_uid,
                        r.instance_count, list(r.sop_instance_uids),
                        r.series_pixel_digest, r.lossy_compressed,
                        r.accession_number_hash, Jsonb(r.acquisition.as_json()),
                        r.corpus_generation,
                    )
                    for r in sorted(records, key=lambda x: x.sort_key)
                ],
            )
    except Exception:
        # MOS-TRAIN-208: "A failure at any step MUST leave no `dataset_versions` row and
        # no manifest object." The row is gone with the transaction; the object is
        # compensated here. See `medos/medos/evidence/store.py` for why the two cannot be one
        # transaction and why this ordering is the safe one.
        store.delete(bucket, key)
        raise

    # `case_count` counts STUDIES, not series: chapter 7 keys a case at study level and
    # `patient_count <= case_count` is a database CHECK that a series count would satisfy
    # accidentally rather than by meaning the right thing.
    return SealResult(
        id=version_id, public_id=public_id, version=version,
        manifest_digest=manifest_digest, manifest_bucket=bucket,
        manifest_object_key=key, case_count=len(studies), patient_count=n_patients,
        acquisition_profile=profile, stratification=report.as_dict(),
        reused=False, gates=gates,
    )


def _current_tenant(conn: psycopg.Connection[Any]) -> str:
    """The bound tenant, read through the chokepoint's own transaction.

    Never `medos.db.tenancy.current_tenant()` directly: the object key must match the
    tenant the ROW will be written under, and the authority on that is the database's
    `current_tenant_id()`, not a ContextVar that a caller could have rebound.
    """
    with tenant_tx(conn):
        return str(_row(conn.execute("SELECT current_tenant_id() AS t").fetchone())["t"])


def mark_defective(
    conn: psycopg.Connection[Any], *, version_id: str, reason: str
) -> None:
    """`MOS-EVID-014`: a sealed object MAY be marked defective but MUST NOT be edited.

    The cascade the requirement mandates -- `INVALIDATED` on every bound `EvaluationRun`
    and `REVOKED` on every citing `ValidationReport` -- is NOT performed here, because
    neither table exists in this migration. It belongs with them, and doing half of it
    here would leave a deployment in which marking a version defective silently left its
    reports ACTIVE. REPORTED as an interface the evaluation component must implement.
    """
    if not reason:
        raise ValueError("MOS-EVID-014: a DEFECTIVE version MUST carry a defect_reason")
    with tenant_tx(conn):
        conn.execute(
            "UPDATE dataset_versions SET status = 'DEFECTIVE', defect_reason = %s, "
            "usable_for_new_runs = false WHERE id = %s",
            (reason, version_id),
        )


def verify_manifest(
    conn: psycopg.Connection[Any], *, version_id: str, store: ManifestStore
) -> bool:
    """`MOS-EVID-015`'s reader-side check.

    "a row whose manifest location does not resolve to an object whose SHA-256 equals
    `manifest_digest` MUST be treated as `DEFECTIVE` by every reader." This returns the
    verdict; `mark_defective()` records it. The two are separate because a transient
    object-store outage is not a defective dataset, and a reader that marked on every
    failed GET would destroy a cohort's status over a network blip.
    """
    with tenant_tx(conn):
        row = _row(
            conn.execute(
                "SELECT manifest_bucket, manifest_object_key, manifest_digest "
                "FROM dataset_versions WHERE id = %s",
                (version_id,),
            ).fetchone()
        )
    body = store.get(row["manifest_bucket"], row["manifest_object_key"])
    return _digest.sha256_of(body) == row["manifest_digest"]


def load_records(
    conn: psycopg.Connection[Any], dataset_version_id: str
) -> list[SeriesRecord]:
    """Rehydrate the sealed manifest from `dataset_cases`. MOS-STORE-291.

    Reads UIDs, not foreign keys into the imaging projection, so this works after the
    projection is rebuilt, after the data moves to another PACS, and after the source
    study is purged from this deployment -- which is the whole reason `dataset_cases`
    stores UIDs.

    `dhash64` comes back `None`: the perceptual hash is not a manifest member and no
    column carries it, so leakage check L4 reports `skipped` for a split frozen from
    rehydrated records. That is visible in `leakage_report` rather than silent, and it is
    the limitation this package reports rather than papers over.
    """
    with tenant_tx(conn):
        rows = conn.execute(
            """
            SELECT patient_key, study_instance_uid, series_instance_uid, modality,
                   sop_class_uid, sop_instance_uids, series_pixel_digest,
                   lossy_compressed, accession_number_hash, acquisition, corpus_generation
              FROM dataset_cases
             WHERE dataset_version_id = %s
             ORDER BY patient_key, study_instance_uid, series_instance_uid
            """,
            (dataset_version_id,),
        ).fetchall()
    out: list[SeriesRecord] = []
    for raw in rows:
        r = _row(raw)
        acq = dict(r["acquisition"] or {})
        spacing = acq.get("pixel_spacing_mm")
        out.append(
            SeriesRecord(
                patient_key=r["patient_key"],
                study_instance_uid=r["study_instance_uid"],
                series_instance_uid=r["series_instance_uid"],
                modality=r["modality"],
                sop_class_uid=r["sop_class_uid"],
                sop_instance_uids=tuple(r["sop_instance_uids"]),
                series_pixel_digest=r["series_pixel_digest"],
                lossy_compressed=r["lossy_compressed"],
                acquisition=Acquisition(
                    slice_thickness_mm=acq.get("slice_thickness_mm"),
                    pixel_spacing_mm=tuple(spacing) if spacing else None,
                    convolution_kernel=acq.get("convolution_kernel"),
                    convolution_kernel_class=acq.get("convolution_kernel_class"),
                    manufacturer=acq.get("manufacturer"),
                    manufacturer_model_name=acq.get("manufacturer_model_name"),
                    kvp=acq.get("kvp"),
                    contrast_phase=acq.get("contrast_phase"),
                    z_coverage_mm=acq.get("z_coverage_mm"),
                    image_type=tuple(acq["image_type"]) if acq.get("image_type") else None,
                    patient_age_years=acq.get("patient_age_years"),
                    patient_sex=acq.get("patient_sex"),
                    body_part_examined=acq.get("body_part_examined"),
                ),
                accession_number_hash=r["accession_number_hash"],
                corpus_generation=int(r["corpus_generation"]),
            )
        )
    return out


# =====================================================================================
# dataset_splits -- THE FREEZE
# =====================================================================================
@dataclass(frozen=True)
class SplitResult:
    id: str
    public_id: str
    split_digest: str
    manifest_bucket: str
    manifest_object_key: str
    partition_patients: dict[str, int]
    leakage_report: dict[str, Any]


def freeze_split(
    conn: psycopg.Connection[Any],
    *,
    dataset_version_id: str,
    name: str,
    assignments: Sequence[tuple[str, str]],
    store: ManifestStore,
    bucket: str,
    frozen_by: str,
    assignment_method: str,
    stratified_by: Sequence[str] = (),
    folds: Mapping[str, int] | None = None,
    strata: Mapping[str, Mapping[str, Any]] | None = None,
    exclusion_reasons: Mapping[str, str] | None = None,
    waivers: Sequence[Mapping[str, Any]] = (),
    records: Sequence[SeriesRecord] | None = None,
    series_without_pixel_evidence: Sequence[str] = (),
) -> SplitResult:
    """Freeze a patient-level split. `MOS-EVID-028` through `MOS-EVID-037`.

    `assignments` is a sequence of `(patient_key, partition)` PAIRS, never a mapping: a
    mapping cannot represent one patient assigned to two partitions, which is the exact
    defect L1 exists to catch, and an argument type that silently repairs the bug is the
    worst possible place to repair it.

    Refuses with `FreezeRefused` when any of L1-L5 is `fail` and unwaived
    (`MOS-EVID-034`), and when coverage is incomplete (`MOS-EVID-031`): "Every
    `patient_key` present in the DatasetVersion manifest MUST appear exactly once in the
    split manifest ... silent omission MUST be a write-time error."

    There is no `seed` parameter and there MUST NOT be one (`MOS-EVID-028`). There is no
    `min_days_between_studies`, no `study_level_split` and no `allow_same_patient`
    (`MOS-TRAIN-117`); the integration test greps for all four.

    `series_without_pixel_evidence` is forwarded to `leakage_report` and is the only way
    a caller can tell L3 that a `series_pixel_digest` it is handing over was not derived
    from pixels. See that function for why the alternative -- a `pass` stored verbatim
    in an immutable row -- is the one outcome this freeze must not be able to record.
    """
    refusals: list[Refusal] = []
    cohort = list(records) if records is not None else load_records(conn, dataset_version_id)
    cohort_patients = {r.patient_key for r in cohort}

    # ---- MOS-EVID-029: the unit of assignment is patient_key ------------------------
    assigned = [pk for pk, _ in assignments]
    unknown = sorted(set(assigned) - cohort_patients)
    missing = sorted(cohort_patients - set(assigned))
    duplicated = sorted({pk for pk in assigned if assigned.count(pk) > 1})

    if missing:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-031",
                code="split_does_not_cover_cohort",
                message=(
                    "every patient_key in the DatasetVersion manifest MUST appear exactly "
                    "once in the split manifest; a subset MUST declare the excluded "
                    "patients explicitly with partition='excluded' and an "
                    "exclusion_reason. Silent omission is a write-time error"
                ),
                observed=len(missing),
                bound=0,
                detail={"missing_patient_keys": missing[:50]},
            )
        )
    if unknown:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-031",
                code="split_names_patients_outside_the_cohort",
                message="a split manifest MUST NOT assign a patient the sealed cohort "
                        "does not contain",
                observed=len(unknown),
                bound=0,
                detail={"unknown_patient_keys": unknown[:50]},
            )
        )

    # ---- MOS-EVID-034: L1-L5, at freeze time ----------------------------------------
    # `series_without_pixel_evidence` names the series whose `series_pixel_digest` was
    # NOT computed from pixels, so that L3 reports `skipped` rather than a `pass` over a
    # comparison that examined nothing. `medos/medos/evidence/leakage.py::leakage_report`
    # carries the full argument; the short form is that `MOS-EVID-035` stores this
    # report verbatim in an IMMUTABLE row (`MOS-EVID-013`), and a `pass` written there
    # is a permanent claim that near-identical images were ruled out. Default-off:
    # every existing caller passes nothing and gets exactly the previous behaviour.
    report = _leak.leakage_report(
        cohort,
        assignments,
        waivers=waivers,
        series_without_pixel_evidence=series_without_pixel_evidence,
    )
    refusals.extend(_leak.blocking_refusals(report))

    if duplicated and not any(r.check_id == "L1" for r in refusals):
        # A patient listed twice for the SAME partition is not a leak, but it breaks
        # `PK (split_id, patient_key)` and MOS-EVID-031's "exactly once".
        refusals.append(
            Refusal(
                check_id="MOS-EVID-031",
                code="patient_listed_more_than_once",
                message="every patient_key MUST appear exactly once in the split manifest",
                observed=len(duplicated), bound=0,
                detail={"patient_keys": duplicated[:50]},
            )
        )

    # ---- MOS-EVID-031: an excluded patient carries a reason -------------------------
    reasons = dict(exclusion_reasons or {})
    unreasoned = sorted(
        pk for pk, part in assignments if part == "excluded" and not reasons.get(pk)
    )
    if unreasoned:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-031",
                code="exclusion_without_reason",
                message="an excluded patient MUST carry an exclusion_reason",
                observed=len(unreasoned), bound=0,
                detail={"patient_keys": unreasoned[:50]},
            )
        )

    if refusals:
        raise FreezeRefused(tuple(refusals))

    lines = split_manifest_lines(
        assignments,
        folds=dict(folds or {}),
        strata={k: dict(v) for k, v in (strata or {}).items()},
        exclusion_reasons=reasons,
    )
    split_digest = _digest.manifest_digest(lines)
    key = manifest_key(_current_tenant(conn), "splits", split_digest)
    store.put(bucket, key, _digest.manifest_bytes(lines))

    partitions = sorted({p for _, p in assignments})
    partition_patients = {p: sum(1 for _, q in assignments if q == p) for p in partitions}
    public_id = _digest.new_split_id()
    leak_json = report.as_dict()

    try:
        with tenant_tx(conn):
            row = _row(
                conn.execute(
                    """
                    INSERT INTO dataset_splits (
                      public_id, tenant_id, dataset_version_id, name, partitions,
                      partition_patients, assignment_method, stratified_by,
                      leakage_report, manifest_bucket, manifest_object_key,
                      manifest_digest, sealed_by)
                    VALUES (%s, current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s)
                    RETURNING id
                    """,
                    (public_id, dataset_version_id, name, partitions,
                     Jsonb(partition_patients), assignment_method, list(stratified_by),
                     Jsonb(leak_json), bucket, key, split_digest, frozen_by),
                ).fetchone()
            )
            split_id = str(row["id"])
            conn.cursor().executemany(
                """
                INSERT INTO dataset_split_members (
                  tenant_id, split_id, patient_key, partition, exclusion_reason, fold,
                  stratum)
                VALUES (current_tenant_id(), %s, %s, %s, %s, %s, %s)
                """,
                [
                    (
                        split_id, pk, part,
                        reasons.get(pk) if part == "excluded" else None,
                        (folds or {}).get(pk) if part != "excluded" else None,
                        Jsonb(dict((strata or {}).get(pk) or {})) if strata else None,
                    )
                    for pk, part in sorted(assignments)
                ],
            )
    except Exception:
        store.delete(bucket, key)
        raise

    return SplitResult(
        id=split_id, public_id=public_id, split_digest=split_digest,
        manifest_bucket=bucket, manifest_object_key=key,
        partition_patients=partition_patients, leakage_report=leak_json,
    )


# =====================================================================================
# annotation_sets -- THE FREEZE, again
# =====================================================================================
@dataclass(frozen=True)
class AnnotationSetResult:
    id: str
    public_id: str
    annotation_digest: str
    manifest_bucket: str
    manifest_object_key: str
    reader_count: int


def freeze_annotation_set(
    conn: psycopg.Connection[Any],
    *,
    dataset_version_id: str,
    name: str,
    capability_id: str,
    label_definition_id: str,
    annotation_type: str,
    consensus_rule: str,
    readers: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    store: ManifestStore,
    bucket: str,
    frozen_by: str,
    consensus_params: Mapping[str, Any] | None = None,
    reference_of_record: bool = False,
) -> AnnotationSetResult:
    """Freeze an `AnnotationSet`. `MOS-EVID-038` through `MOS-EVID-046`, `MOS-TRAIN-110`.

    The consensus rule is validated HERE and not only by the database CHECKs, because
    `MOS-EVID-039`'s parameter requirements are per-rule and a CHECK cannot express them
    all: `majority_at_least_2` needs `consensus_params.min_agreeing`, `staple` needs its
    five reproducibility parameters, `arbitrated` needs the arbitrator's `reader_id`.
    `MOS-TRAIN-105` is why `staple`'s five are mandatory -- the reduction "MUST be
    reproducible from the persisted per-reader masks alone", and a tool-side reduction
    that records none of them cannot be re-derived.

    `MOS-TRAIN-110`: the pipeline MUST NOT append to a frozen set; adding a reader or a
    case produces a NEW AnnotationSet bound to the same DatasetVersion. The database
    enforces that with `annotation_sets_frozen` and `annotations_frozen`; there is no
    `add_reader()` in this module and one MUST NOT be added.
    """
    params = dict(consensus_params or {})
    refusals: list[Refusal] = []

    if not readers:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-038",
                code="annotation_set_names_no_reader",
                message="every AnnotationSet MUST name at least one reader; a set with "
                        "reader_count = 0 MUST be refused",
                observed=0, bound=1,
            )
        )
    reader_count = len(readers)

    required_params: dict[str, tuple[str, ...]] = {
        "majority_at_least_2": ("min_agreeing",),
        "staple": ("iterations", "convergence_tol", "initial_sensitivity",
                   "initial_specificity", "rng_seed"),
        "arbitrated": ("arbitrator_id",),
    }
    missing = [p for p in required_params.get(consensus_rule, ()) if p not in params]
    if missing:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-039",
                code="consensus_params_incomplete",
                message=f"consensus_rule {consensus_rule!r} MUST record {missing}",
                observed=sorted(params), bound=list(required_params[consensus_rule]),
            )
        )
    if consensus_rule == "single_reader" and reader_count != 1:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-039", code="single_reader_with_many_readers",
                message="`single_reader` requires reader_count = 1",
                observed=reader_count, bound=1,
            )
        )
    if consensus_rule == "majority_at_least_2" and reader_count < 3:
        refusals.append(
            Refusal(
                check_id="MOS-EVID-039", code="majority_needs_three_readers",
                message="`majority_at_least_2` requires reader_count >= 3",
                observed=reader_count, bound=3,
            )
        )
    # MOS-EVID-042: an algorithmic reference standard is never the reference of record.
    if reference_of_record and any(r.get("role") == "algorithm" for r in readers):
        refusals.append(
            Refusal(
                check_id="MOS-EVID-042", code="algorithmic_reference_of_record",
                message="if the only available reference standard is algorithmic, the "
                        "reader role MUST be `algorithm` and reference_of_record MUST be "
                        "false; an AcceptanceCriteria gate refuses a set whose flag is "
                        "false",
                observed=True, bound=False,
            )
        )
    if refusals:
        raise FreezeRefused(tuple(refusals))

    lines = annotation_manifest_lines(entries)
    annotation_digest = _digest.manifest_digest(lines)
    key = manifest_key(_current_tenant(conn), "annotations", annotation_digest)
    store.put(bucket, key, _digest.manifest_bytes(lines))
    public_id = _digest.new_annotation_set_id()

    try:
        with tenant_tx(conn):
            row = _row(
                conn.execute(
                    """
                    INSERT INTO annotation_sets (
                      public_id, tenant_id, dataset_version_id, name, capability_id,
                      label_definition_id, annotation_type, consensus_rule,
                      consensus_params, reader_count, reference_of_record,
                      manifest_bucket, manifest_object_key, manifest_digest, sealed_by)
                    VALUES (%s, current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (public_id, dataset_version_id, name, capability_id,
                     label_definition_id, annotation_type, consensus_rule, Jsonb(params),
                     reader_count, reference_of_record, bucket, key, annotation_digest,
                     frozen_by),
                ).fetchone()
            )
            set_id = str(row["id"])
            conn.cursor().executemany(
                """
                INSERT INTO annotation_readers (
                  tenant_id, annotation_set_id, reader_id, role, years_experience,
                  board_certified, specialty, tool, instructions_uri, blinded_to)
                VALUES (current_tenant_id(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                [
                    (set_id, r["reader_id"], r["role"], r.get("years_experience"),
                     r.get("board_certified"), r.get("specialty"), r["tool"],
                     r["instructions_uri"], list(r.get("blinded_to") or []))
                    for r in readers
                ],
            )
    except Exception:
        store.delete(bucket, key)
        raise

    return AnnotationSetResult(
        id=set_id, public_id=public_id, annotation_digest=annotation_digest,
        manifest_bucket=bucket, manifest_object_key=key, reader_count=reader_count,
    )


# =====================================================================================
# THE READ SIDE. Table 10.2-B rows R28, R30, R31, 32 and 37.
#
# `medos/api/v1/routes.yaml` recorded every one of these as absent under `engine_absent:`:
# `freeze_split` writes the row and nothing reads one back; `freeze_annotation_set`
# writes `annotation_sets` and `annotation_readers` and nothing reads either; nothing
# lists datasets; nothing reads a version row back by id. This section is that gap
# closed, and it is nothing but SELECTs under the tenant GUC.
#
# THEY RETURN ROWS, NOT WIRE SHAPES. `medos/medos/api/routes_curation.py` projects. The split
# is the one `medos/medos/api/routes_training.py` already draws: which columns reach a client
# is a decision about the CONTRACT (`MOS-API-084`'s schemas), and a projection written
# here would put that decision where nobody reading the route can see it.
#
# WHY NONE OF THEM RAISES. A missing row and another tenant's row are indistinguishable
# under FORCE ROW LEVEL SECURITY, by construction, and that is what `MOS-SEC-072` is
# for. Each returns `None` or an empty list and the handler renders one `404` for both;
# a `LookupError` whose message named the id would be the same disclosure wearing a
# stack trace.
#
# ORDERING is `MOS-API-018`'s `created_at DESC, id DESC` on every listing and is not
# client-selectable. There is NO cursor: `MOS-API-017` makes it an opaque keyset over
# the sort key, the tie-break id and a fingerprint of the filter set, and implementing
# that here rather than once, generically, is how two incompatible cursor formats come
# to exist. The routes report `has_more` honestly and `next_cursor: null`; the debt is
# named on each row in the change report, exactly as `list_training_runs` names its own.
# =====================================================================================
_DATASET_COLUMNS = (
    "id, public_id, slug, display_name, purpose, custodian, visibility, "
    "licence_spdx, licence_text, licence_url, created_at"
)

_VERSION_COLUMNS = (
    "id, public_id, dataset_id, version, parent_version_id, manifest_digest, "
    "manifest_line_count, case_count, patient_count, study_count, series_count, "
    "instance_count, deidentification_status, status, erasure_state, "
    "usable_for_new_runs, sealed_at, sealed_by"
)

_SPLIT_COLUMNS = (
    "id, public_id, dataset_version_id, name, partition_level, partitions, "
    "partition_patients, assignment_method, stratified_by, leakage_report, "
    "manifest_digest, sealed_at, sealed_by"
)

_ANNOTATION_SET_COLUMNS = (
    "id, public_id, dataset_version_id, name, capability_id, label_definition_id, "
    "annotation_type, consensus_rule, consensus_params, reader_count, "
    "reference_of_record, manifest_digest, sealed_at, sealed_by"
)


def _uuid_or_text(value: str) -> Any:
    """A `uuid.UUID` when the string is one, else the string unchanged.

    psycopg binds a Python `str` to `text`, and `text = uuid` has no operator in
    Postgres -- so a handler passing a well-formed uuid STRING would get `42883` rather
    than a row. Converting at the boundary keeps every call site above free of the
    detail; a malformed value is handed through unchanged and matches nothing, which is
    the same answer as a row this tenant may not read.
    """
    import uuid as _u

    try:
        return _u.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return value


def list_datasets(
    conn: psycopg.Connection[Any],
    *,
    purpose: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Table 10.2-B row 32. The list row R26's `dataset_id` is CHOSEN from.

    Declared because `MOS-UI-101` forbids the operator composing an id, and row 33
    (`POST /datasets`) is deliberately NOT served: it binds `dataset.write`, which
    section 8.3.2 registers as a spelling a build MUST fail on and `MOS-UI-104` forbids
    this surface propagating. Recording a `Dataset` -- one source, one licence
    (`MOS-EVID-027`) -- stays a legal act by the site.
    """
    where = " AND purpose = %s" if purpose else ""
    params: tuple[Any, ...] = (purpose, limit) if purpose else (limit,)
    with tenant_tx(conn):
        rows = conn.execute(
            f"SELECT {_DATASET_COLUMNS} FROM datasets "
            f"WHERE tenant_id = current_tenant_id(){where} "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_dataset_version(
    conn: psycopg.Connection[Any], dataset_version_id: str
) -> dict[str, Any] | None:
    """Table 10.2-B row 37. `MOS-UI-147`'s read-only cohort summary, one SELECT.

    Rows 36, 38 and 40 are NOT served, and the reason belongs next to this function:
    they describe a two-step model -- create an UNSEALED version, then seal it -- that
    this platform does not implement. `seal_dataset_version` mints the version already
    sealed from the records it is handed, so there is no unsealed version to create, to
    seal or to delete.
    """
    with tenant_tx(conn):
        row = conn.execute(
            f"SELECT {_VERSION_COLUMNS} FROM dataset_versions "
            "WHERE tenant_id = current_tenant_id() AND id = %s",
            (_uuid_or_text(dataset_version_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def get_split(
    conn: psycopg.Connection[Any], dataset_split_id: str
) -> dict[str, Any] | None:
    """Table 10.2-B row R28. The WHOLE `leakage_report`, and never a verdict.

    `MOS-EVID-035` stores it verbatim, `MOS-EVID-036` requires every waiver reproduced
    in full in any `ValidationReport` citing the split, and `MOS-EVID-037` makes a
    `skipped` L4 a permanent property of it -- so a projection carrying only
    `frozen: true` would hide, one screen after the `SealRun` made it visible, exactly
    the thing the `SealRun` exists to show.
    """
    column = "public_id" if str(dataset_split_id).startswith("spl_") else "id"
    param: Any = (
        dataset_split_id if column == "public_id" else _uuid_or_text(dataset_split_id)
    )
    with tenant_tx(conn):
        row = conn.execute(
            f"SELECT {_SPLIT_COLUMNS} FROM dataset_splits "
            f"WHERE tenant_id = current_tenant_id() AND {column} = %s",
            (param,),
        ).fetchone()
    return dict(row) if row is not None else None


def list_annotation_sets(
    conn: psycopg.Connection[Any], *, dataset_version_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """Table 10.2-B row R30, so that R6's `annotation_set_id` is CHOSEN, not composed."""
    with tenant_tx(conn):
        rows = conn.execute(
            f"SELECT {_ANNOTATION_SET_COLUMNS} FROM annotation_sets "
            "WHERE tenant_id = current_tenant_id() AND dataset_version_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (_uuid_or_text(dataset_version_id), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_annotation_set(
    conn: psycopg.Connection[Any], annotation_set_id: str
) -> dict[str, Any] | None:
    """Table 10.2-B row R31: one frozen set, with its readers JOINED and not counted.

    The readers are projected rather than reduced to a count because `MOS-EVID-043` puts
    inter-reader agreement in every report citing a multi-reader set and `MOS-UI-125`
    requires the console to have said so before the campaign started: a Dice of 0.82
    against a reference whose own inter-reader Dice is 0.84 is a different claim from
    the same 0.82 against 0.97.
    """
    column = "public_id" if str(annotation_set_id).startswith("ann_") else "id"
    param: Any = (
        annotation_set_id if column == "public_id" else _uuid_or_text(annotation_set_id)
    )
    with tenant_tx(conn):
        row = conn.execute(
            f"SELECT {_ANNOTATION_SET_COLUMNS} FROM annotation_sets "
            f"WHERE tenant_id = current_tenant_id() AND {column} = %s",
            (param,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        readers = conn.execute(
            "SELECT reader_id, role, years_experience, board_certified, specialty, "
            "tool, instructions_uri, blinded_to FROM annotation_readers "
            "WHERE tenant_id = current_tenant_id() AND annotation_set_id = %s "
            "ORDER BY reader_id",
            (out["id"],),
        ).fetchall()
    out["readers"] = [dict(r) for r in readers]
    return out


def readers_of(
    conn: psycopg.Connection[Any], annotation_set_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Every named reader of several sets, in ONE query. For row R30's page.

    One query and not N: a page of twenty sets that issued twenty-one round trips would
    be the classic shape, and `MOS-EVID-038`'s readers are few enough per set that the
    join costs nothing.
    """
    if not annotation_set_ids:
        return {}
    with tenant_tx(conn):
        rows = conn.execute(
            "SELECT annotation_set_id, reader_id, role, years_experience, "
            "board_certified, specialty, tool, instructions_uri, blinded_to "
            "FROM annotation_readers "
            "WHERE tenant_id = current_tenant_id() AND annotation_set_id = ANY(%s) "
            "ORDER BY annotation_set_id, reader_id",
            ([_uuid_or_text(i) for i in annotation_set_ids],),
        ).fetchall()
    out: dict[str, list[dict[str, Any]]] = {}
    for raw in rows:
        r = dict(raw)
        out.setdefault(str(r.pop("annotation_set_id")), []).append(r)
    return out
