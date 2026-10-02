# SPDX-License-Identifier: Apache-2.0
"""Registering the candidate, and the two lifecycle transitions the pipeline may cause.

`MOS-TRAIN-138`: "On `state = SUCCEEDED`, the pipeline MUST register the bundle as a new
`ModelVersion` at `lifecycle_status = REGISTERED` (`MOS-REG-021`, permission
`artifact.publish`), with `spec.derived_from = null`, `spec.preprocessing_spec_ref` naming
the registered spec, `spec.golden_fixture` populated per `MOS-TRAIN-134`,
`spec.not_validated_for` and `spec.known_failure_modes` non-empty (`MOS-REG-032`), and
`spec.evaluation_run_id` null. The `TrainingRun.candidate_model_version_id` MUST be set in
the same transaction, so that every candidate has exactly one producing run and every run
has at most one candidate."

`MOS-TRAIN-139`: "Starting the candidate `EvaluationRun` of 17.8 MUST transition the
candidate to `VALIDATING` (`MOS-REG-021`, permission `evidence.run`), and its completion
MUST transition it to `VALIDATED` or back to `REGISTERED`. `VALIDATING` and `VALIDATED` are
the **only** statuses this pipeline can cause. The pipeline's service account MUST hold
`artifact.publish` and `evidence.run` and MUST NOT hold `artifact.approve`. This is the
architectural ruling of the chapter expressed where it can be tested: as a row in the grant
table."

So this module exposes exactly three verbs -- `register_candidate`, `begin_validation`,
`finish_validation` -- and `PIPELINE_PERMISSIONS` names the two grants they need.
`APPROVE` is not among them and there is no function here that reaches it.

TWO SPECIFICATION CONTRADICTIONS THAT BLOCK `spec.evaluation_run_id: null`, REPORTED
-------------------------------------------------------------------------------------
`MOS-TRAIN-138` requires a candidate to be registered at `REGISTERED` with
`spec.evaluation_run_id` NULL -- necessarily so, because `MOS-TRAIN-139` puts the creation
of the run strictly after the registration. Chapter 6 disagrees twice:

  (a) `MOS-REG-030`'s manifest carries `evaluation_run_id` as a required, non-nullable
      member, and `medos/medos/registry/schemas.py` renders it faithfully as
      `{"type": "string", "pattern": ...}` inside `required`. A manifest with
      `evaluation_run_id: null` is therefore refused by the registry, and a candidate
      cannot be registered in the state `MOS-TRAIN-138` requires. `derived_from` IS typed
      nullable in the same schema, so this is a per-field omission and not a reading of
      the whole manifest.
  (b) Chapter 6 writes the id as `er_01JP4T9X7B` (`06-registries.md` lines 347, 358, 722,
      951) and chapter 7 section 7.2 writes it as `evr_<ULID>`; 0007 renders chapter 7's
      spelling as `CHECK (public_id ~ '^evr_[0-9A-HJKMNP-TV-Z]{26}$')`. The registry's
      pattern `^er_[0-9A-HJKMNP-TV-Z]{10,26}$` does not match `evr_...`, so no real
      `evaluation_runs.public_id` from this platform can be written into a
      `model_version` manifest at all.

Neither is patched here. `candidate_manifest()` builds exactly what `MOS-TRAIN-138`
describes, and `register_candidate()` refuses with both requirement ids named when the
registry rejects it -- a loud failure, which `MOS-REL-050` requires of a path that is not
yet implementable ("A path that is not yet implemented MUST fail loudly, not return a
default"). Passing an `evaluation_run_ref` that the registry's own pattern admits
registers the candidate normally, so everything downstream of registration -- the two
transitions, the producing-run binding, the audit trail -- ships and is tested. What is
blocked is precisely and only the `null` clause. Both defects are in this component's
report with the one-line fix for each.

Spec: MOS-TRAIN-129, MOS-TRAIN-130, MOS-TRAIN-134, MOS-TRAIN-137, MOS-TRAIN-138,
MOS-TRAIN-139, MOS-TRAIN-174, MOS-REG-021, MOS-REG-030 to MOS-REG-032, MOS-REG-036,
MOS-REG-084, MOS-REG-103.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

import psycopg

from medos.db import audit
from medos.db.tenancy import tenant_tx
from medos.registry import repo as registry_repo
from medos.registry.digest import content_digest_of
from medos.registry.errors import InvalidManifest
from medos.sdk.errors import Refusal, RunRefused, TrainingError
from medos.training.runs import RunRow
from medos.training.runs import get as get_run

__all__ = [
    "FORBIDDEN_PIPELINE_PERMISSIONS",
    "PIPELINE_PERMISSIONS",
    "PIPELINE_STATUSES",
    "begin_validation",
    "candidate_manifest",
    "finish_validation",
    "register_candidate",
]

#: `MOS-TRAIN-139`: "The pipeline's service account MUST hold `artifact.publish` and
#: `evidence.run`". Two, and no third.
PIPELINE_PERMISSIONS: Final[tuple[str, ...]] = ("artifact.publish", "evidence.run")

#: "... and MUST NOT hold `artifact.approve`." `MOS-TRAIN-174` widens it to the full set
#: no non-human principal may hold; `medos.training.orchestrator.FORBIDDEN_PERMISSIONS`
#: is the same list for the orchestrator's account, spelled once there and referenced here.
FORBIDDEN_PIPELINE_PERMISSIONS: Final[tuple[str, ...]] = (
    "artifact.approve",
    "evidence.report.issue",
    "deployment.approve_clinical",
    "deployment.promote",
    "deployment.gate.override",
)

#: `MOS-TRAIN-139`: "`VALIDATING` and `VALIDATED` are the **only** statuses this pipeline
#: can cause." `REGISTERED` appears because the pipeline also causes the return edge when
#: a run fails or is abandoned, which `MOS-REG-021` spells `VALIDATING -> REGISTERED`.
PIPELINE_STATUSES: Final[tuple[str, ...]] = ("REGISTERED", "VALIDATING", "VALIDATED")


def _refuse(refusals: Sequence[Refusal]) -> None:
    raise RunRefused(tuple(refusals))


def _one(check_id: str, code: str, message: str, **kw: Any) -> Refusal:
    return Refusal(check_id=check_id, code=code, message=message, **kw)


def candidate_manifest(
    *,
    family: str,
    version: str,
    org_id: str,
    signing_identity: str,
    capability_ids: Sequence[str],
    preprocessing_spec_ref: str,
    golden_fixture: Mapping[str, Any],
    io: Mapping[str, Any],
    runtime: Mapping[str, Any],
    weights: Mapping[str, Any],
    not_validated_for: Sequence[str],
    known_failure_modes: Sequence[str],
    operating_point: Mapping[str, Any] | None = None,
    evaluation_run_ref: str | None = None,
    created_at: datetime | None = None,
    extra_spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`MOS-REG-030`'s manifest for a candidate, in `MOS-TRAIN-138`'s state.

    `evaluation_run_ref` defaults to `None`, which is what `MOS-TRAIN-138` requires and
    what the registry's schema refuses -- see the module docstring. It is a parameter and
    not a constant so that a caller can register a candidate whose run already exists (the
    conversion path of `MOS-TRAIN-172`, where the incumbent IS the source version) without
    this module having to carry two manifest builders.

    `spec.derived_from` is `None` here and only here: a candidate from a `TrainingRun` is
    a new lineage. A conversion sets it, and `MOS-TRAIN-166` fixes what else it may not
    change.
    """
    if not not_validated_for or not known_failure_modes:
        # MOS-REG-032: "An empty list MUST be rejected. 'None known' is expressible only
        # as the literal string entry `no known failure modes have been characterised`,
        # which is a claim the publisher makes explicitly and which appears verbatim in
        # the provenance panel."
        _refuse(
            [
                _one(
                    "MOS-REG-032",
                    "publisher_declaration_empty",
                    "spec.not_validated_for and spec.known_failure_modes MUST each "
                    "contain at least one entry; 'none known' is expressible only as the "
                    "literal sentence, which is a claim the publisher makes",
                    observed={"not_validated_for": len(not_validated_for),
                              "known_failure_modes": len(known_failure_modes)},
                    bound=1,
                )
            ]
        )

    spec: dict[str, Any] = {
        "capabilities": list(capability_ids),
        "weights_availability": "platform_managed",
        "weights": dict(weights),
        "preprocessing_spec_ref": preprocessing_spec_ref,
        "golden_fixture": dict(golden_fixture),
        "io": {"input": dict(io["input"]), "output": dict(io["output"])},
        "runtime": dict(runtime),
        # MOS-TRAIN-138: a candidate from a TrainingRun is a new lineage.
        "derived_from": None,
        "evaluation_run_id": evaluation_run_ref,
        "not_validated_for": list(not_validated_for),
        "known_failure_modes": list(known_failure_modes),
    }
    if operating_point is not None:
        spec["operating_point"] = dict(operating_point)
    if extra_spec:
        spec.update({k: v for k, v in extra_spec.items() if k not in spec})

    stamp = (created_at or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest: dict[str, Any] = {
        "schema_version": "1.0.0",
        "kind": "model_version",
        "family": family,
        "version": version,
        "publisher": {"org_id": org_id, "signing_identity": signing_identity},
        "created_at": stamp,
        "spec": spec,
    }
    # `MOS-REG-017`: the publisher MAY supply it and the registry recomputes and compares.
    # Supplied here so the candidate manifest is complete as it leaves this function --
    # chapter 6's envelope makes the member required, and a manifest that is only valid
    # after somebody else fills a field in is not a manifest this function produced.
    manifest["content_digest"] = content_digest_of(manifest)
    return manifest


def register_candidate(
    conn: psycopg.Connection[Any],
    *,
    run_id: str,
    manifest: Mapping[str, Any],
    public_id: str,
    actor: audit.Actor,
    trace_id: str,
    oci_image_digest: str,
    signature: bytes,
    signature_alg: str,
    signer_identity: str,
    sbom_object_key: str,
    bundle: Mapping[str, Any] | None = None,
    request_id: str | None = None,
    tenant_id: str | None = None,
) -> tuple[dict[str, Any], RunRow]:
    """Register the bundle as a `ModelVersion` at `REGISTERED` and bind it to the run.

    One transaction, as `MOS-TRAIN-138` requires: the artifact row, its changelog row, its
    `AuditEvent` and `training_runs.candidate_model_version_id` commit together or not at
    all. A candidate with no producing run, or a run with two candidates, is not a state
    this function can leave behind.

    The supply-chain material is not optional and has no default. `MOS-REG-090`: a
    verification error "MUST NOT degrade to a warning, and MUST NOT be bypassable by
    configuration", and a candidate entering `REGISTERED` is exactly the transition
    `MOS-REG-021` annotates "signature verified".
    """
    run = get_run(conn, run_id, tenant_id=tenant_id)
    if run.state != "SUCCEEDED":
        _refuse(
            [
                _one(
                    "MOS-TRAIN-138",
                    "candidate_needs_a_succeeded_run",
                    f"the producing run is {run.state}; MOS-TRAIN-124 says a run missing "
                    "any binding field MUST NOT reach SUCCEEDED and its output MUST NOT "
                    "be registrable as a ModelVersion",
                    observed=run.state,
                    bound="SUCCEEDED",
                )
            ]
        )
    if run.get("candidate_model_version_id") is not None:
        _refuse(
            [
                _one(
                    "MOS-TRAIN-138",
                    "run_already_has_a_candidate",
                    "every run has at most one candidate; a second registration would "
                    "give a ModelVersion a producing run it was not produced by",
                    observed=str(run.get("candidate_model_version_id")),
                )
            ]
        )
    if run.bundle_digest is None:  # pragma: no cover - the CHECK already refused
        _refuse(
            [_one("MOS-TRAIN-129", "no_bundle", "a SUCCEEDED run has a bundle digest")]
        )

    with tenant_tx(conn, tenant_id) as tx:
        try:
            row, created = registry_repo.publish(
                conn,
                manifest=manifest,
                public_id=public_id,
                actor=actor,
                trace_id=trace_id,
                request_id=request_id,
                lifecycle_status="REGISTERED",
                oci_image_digest=oci_image_digest,
                signature=signature,
                signature_alg=signature_alg,
                signer_identity=signer_identity,
                sbom_object_key=sbom_object_key,
                bundle=dict(bundle or {}),
            )
        except InvalidManifest as exc:
            _raise_schema_conflict_if_that_is_what_it_is(exc, manifest)
            raise

        tx.execute(
            "UPDATE training_runs SET candidate_model_version_id = %s WHERE id = %s",
            (row["id"], _uuid.UUID(run.id)),
        )
        updated = get_run(conn, run.id, tenant_id=tenant_id)

    if not created:
        # MOS-REG-019's idempotent republish reached a row this run did not produce.
        raise TrainingError(
            f"{public_id} was already published with byte-identical content; a candidate "
            "MUST be a new version, because MOS-TRAIN-138 binds it to exactly one "
            "producing run"
        )
    return dict(row), updated


def _raise_schema_conflict_if_that_is_what_it_is(
    exc: InvalidManifest, manifest: Mapping[str, Any]
) -> None:
    """Turn the registry's schema rejection into a refusal that names the contradiction.

    Only fires for the one violation the module docstring documents. Any other schema
    violation is re-raised unchanged, because it is the publisher's to fix.
    """
    spec = dict(manifest.get("spec") or {})
    if spec.get("evaluation_run_id") is not None:
        return
    violations = [str(v) for v in (exc.extensions.get("violations") or ())]
    if not any("evaluation_run_id" in v for v in violations):
        return
    _refuse(
        [
            _one(
                "MOS-TRAIN-138",
                "candidate_cannot_be_registered_with_a_null_evaluation_run",
                "MOS-TRAIN-138 requires a candidate to be registered at REGISTERED with "
                "spec.evaluation_run_id NULL -- necessarily, because MOS-TRAIN-139 puts "
                "the candidate EvaluationRun strictly AFTER the registration. "
                "MOS-REG-030's manifest types that member as a required non-nullable "
                "string, so the registry refuses it. The two requirements cannot both be "
                "satisfied and this is reported as a specification defect rather than "
                "worked around: the fix is one line in chapter 6's model_version schema, "
                "typing evaluation_run_id as ['string','null'] exactly as derived_from "
                "already is. Pass evaluation_run_ref= to register a candidate whose run "
                "already exists",
                observed=None,
                bound="a non-null evaluation_run_id, per MOS-REG-030",
                detail={
                    "conflicting_requirements": ["MOS-TRAIN-138", "MOS-REG-030",
                                                 "MOS-REG-031"],
                    "registry_violations": violations[:8],
                    "second_defect": (
                        "chapter 6 writes the id as er_<...> (06-registries.md:358) and "
                        "chapter 7 section 7.2 writes it as evr_<ULID>, which 0007 "
                        "renders as CHECK (public_id ~ '^evr_...'). No real "
                        "evaluation_runs.public_id matches the registry's ^er_ pattern"
                    ),
                },
            )
        ]
    )


def begin_validation(
    conn: psycopg.Connection[Any],
    *,
    model_version_id: str,
    actor: audit.Actor,
    trace_id: str,
    reason: str = "candidate EvaluationRun started (MOS-TRAIN-139)",
    request_id: str | None = None,
) -> dict[str, Any]:
    """`REGISTERED -> VALIDATING`, permission `evidence.run`. `MOS-TRAIN-139`.

    "Starting the candidate `EvaluationRun` of 17.8 MUST transition the candidate to
    `VALIDATING`". Called by the pipeline when the run is created, not when it finishes:
    the status says an evaluation is in flight, which is what makes a candidate that never
    finished visibly distinguishable from one that was never evaluated.
    """
    return registry_repo.set_status(
        conn,
        model_version_id,
        to_status="VALIDATING",
        reason=reason,
        actor=actor,
        trace_id=trace_id,
        request_id=request_id,
        kind="model_version",
    )


def finish_validation(
    conn: psycopg.Connection[Any],
    *,
    model_version_id: str,
    succeeded: bool,
    actor: audit.Actor,
    trace_id: str,
    reason: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """`VALIDATING -> VALIDATED` or back to `REGISTERED`. `MOS-TRAIN-139`.

    `MOS-TRAIN-151` is what makes `VALIDATED` the pipeline's terminus: "The automated
    portion of this chapter therefore terminates at: *a complete, self-verified, unsigned
    report payload with a computed digest and a rendered approval dossier, and a candidate
    sitting at `lifecycle_status = VALIDATED`.* Every step from data to that point is
    machine work and MUST be machine work; nothing past it is."

    There is deliberately no `approve()` in this module. `APPROVED` is `artifact.approve`,
    which is act A2 of `MOS-TRAIN-173` and lives in `medos.promotion` -- a package this
    one does not import.
    """
    to_status = "VALIDATED" if succeeded else "REGISTERED"
    default = (
        "candidate EvaluationRun SUCCEEDED (MOS-TRAIN-139)"
        if succeeded
        else "candidate EvaluationRun failed or was abandoned (MOS-REG-021)"
    )
    return registry_repo.set_status(
        conn,
        model_version_id,
        to_status=to_status,
        reason=reason or default,
        actor=actor,
        trace_id=trace_id,
        request_id=request_id,
        kind="model_version",
    )
