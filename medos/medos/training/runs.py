# SPDX-License-Identifier: Apache-2.0
"""`TrainingRun`: the reproducibility binding, submitted, started, finished. 17.7.2.

`MOS-TRAIN-124`: "`TrainingRun` is an entity of this chapter. It is the training-side
analogue of `EvaluationRun` and it exists for the same reason: it MUST bind every input
that can change the artifact. A run missing any field below MUST NOT reach
`state = SUCCEEDED`, and its output MUST NOT be registrable as a `ModelVersion`."

WHAT "EVERY INPUT THAT CAN CHANGE THE ARTIFACT" IS, AND WHERE EACH ONE COMES FROM
---------------------------------------------------------------------------------
Nine of them, and not one is defaulted by this module:

    dataset version   + digest   the sealed cohort            (0006, MOS-EVID-016)
    split             + digest   the frozen partitioning      (0006, MOS-EVID-028)
    annotation set    + digest   the reference standard       (0006, MOS-EVID-040)
    preprocessing spec+ version + digest                      (ch. 4, MOS-IMG-045)
    code commit       + dirty flag                            (MOS-TRAIN-125)
    container image digest                                    (MOS-TRAIN-124)
    seeds                                                     (MOS-TRAIN-124)
    hardware                                                  (MOS-TRAIN-124)
    framework versions + determinism settings                 (MOS-TRAIN-126)

`RunBinding` carries all nine and `run_digest_of()` digests them under JCS
(`MOS-EVID-008`), so two runs with the same digest are the same experiment and the
tenant-scoped unique key makes an accidental duplicate impossible -- exactly the property
`MOS-STORE-301` states for `evaluation_runs`.

WHAT IS PROMISED AND WHAT IS NOT
--------------------------------
`MOS-TRAIN-126` is the honest half and this module does not overstate it: "Bit-exact
reproducibility of a training run MUST NOT be required and MUST NOT be claimed." There is
no `reproduce()` here and no field asserting determinism was achieved. What the record
guarantees is weaker and sufficient -- "every input is pinned, the determinism settings
actually used are recorded rather than asserted, and a re-run is a *comparable* run rather
than an identical one."

THE THREE THINGS SUBMIT REFUSES, AND WHY THEY ARE REFUSED AT SUBMIT
--------------------------------------------------------------------
`MOS-TRAIN-144`'s argument -- "Failing in the first second of a two-hour GPU run is
materially cheaper than failing at the gate" -- applies to all three:

  1. A leaking split (`MOS-TRAIN-115`). "The run is refused before the first batch is
     loaded, with the violating `patient_key` and both partitions named, and the waiver
     did not permit it." The check is `medos.evidence.leakage.waiver_blocks_training`,
     imported rather than reimplemented, so the rule and the thing it constrains cannot
     drift apart.
  2. A hand-configured backend on a `label` capability with no rationale
     (`MOS-TRAIN-211`). "The rationale is not a gate; it exists so that the first question
     at the approval gate is answerable: *was this compared against the default, or did
     nobody run the default?*"
  3. A binding that is not complete. Every column is `NOT NULL` at insert (0013 section
     2) and this module refuses before the database has to.

`code_dirty = true` is NOT refused at submit. `MOS-TRAIN-125` says it "MUST block
`state = SUCCEEDED`", which is a different statement: a dirty-tree run is a legitimate
experiment and an illegitimate candidate, and refusing it at submit would push people to
commit noise in order to run anything.

WHAT THIS MODULE DOES NOT IMPORT
---------------------------------
`MOS-TRAIN-189`: "There MUST be no path -- no function, no API call, no scheduled task, no
policy -- from a `TrainingRun` ... to a `state = SERVING` `Deployment`". Nothing under
`medos.evidence.deployment` or `medos.promotion` appears in this package's import closure,
and `tests/integration/test_training_run.py` asserts the closure rather than the sentence.

Spec: MOS-TRAIN-115, MOS-TRAIN-116, MOS-TRAIN-124 to MOS-TRAIN-128, MOS-TRAIN-135,
MOS-TRAIN-141, MOS-TRAIN-189, MOS-TRAIN-211, MOS-TRAIN-216, MOS-TRAIN-219, MOS-TRAIN-223,
MOS-EVID-008, MOS-EVID-034, MOS-STORE-301.
"""

from __future__ import annotations

import re
import uuid as _uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import psycopg
from psycopg.types.json import Jsonb

from medos.db import audit
from medos.db.tenancy import current_tenant, tenant_tx
from medos.evidence.leakage import LeakageReport, waiver_blocks_training
from medos.sdk.canonical import canonical_bytes, new_ulid, sha256_hex
from medos.sdk.errors import Refusal, RunRefused, TrainingError

__all__ = [
    "AUTO_CONFIGURING_BACKENDS",
    "BACKEND_KINDS",
    "RunBinding",
    "RunRow",
    "cancel",
    "fail",
    "get",
    "list_runs",
    "note_test_exposure",
    "record_seed_variance",
    "run_digest_of",
    "seed_variance",
    "start",
    "submit",
    "succeed",
    "test_exposure_count",
]

#: `MOS-TRAIN-124`'s two literals plus `auto3dseg`, which `MOS-TRAIN-211` names as one of
#: the two permitted defaults for a `label` capability and `MOS-TRAIN-223` gives its own
#: fingerprint mapping. 0013's CHECK carries the same three values; this is the message.
BACKEND_KINDS: Final[tuple[str, ...]] = ("monai_supervised", "nnunet", "auto3dseg")

#: `MOS-TRAIN-211`: "the default `training_backend.kind` MUST be a dataset-fingerprint
#: auto-configuring backend -- `nnunet` or `auto3dseg`."
AUTO_CONFIGURING_BACKENDS: Final[frozenset[str]] = frozenset({"nnunet", "auto3dseg"})

_COMMIT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"^sha256:[0-9a-f]{64}$")

_SEED_KEYS: Final[tuple[str, ...]] = (
    "python", "numpy", "torch", "dataloader_worker_base",
)
_DETERMINISM_KEYS: Final[tuple[str, ...]] = (
    "torch_use_deterministic_algorithms", "cudnn_benchmark",
    "cublas_workspace_config", "tf32_allowed",
)
_HARDWARE_KEYS: Final[tuple[str, ...]] = (
    "gpu_model", "gpu_count", "driver", "cuda", "cudnn", "nccl",
)
_FRAMEWORK_KEYS: Final[tuple[str, ...]] = ("torch", "monai", "numpy", "simpleitk")

_COLUMNS = """
    id, public_id, tenant_id, capability_id,
    dataset_version_id, dataset_version_digest, split_id, split_digest,
    fit_partition, select_partition, annotation_set_id, annotation_digest,
    preprocessing_spec_id, preprocessing_spec_version, preprocessing_spec_digest,
    code_commit, code_dirty, image_digest, training_backend, backend_rationale,
    hyperparameters, hyperparameters_digest, search_id, trial_index, nominated,
    search_trial_score, seeds, determinism, hardware, framework_versions,
    fingerprint_digest, fingerprint_bucket, fingerprint_object_key,
    state, failure_reason, orchestrator_run_id, started_at, finished_at, runner,
    bundle_digest, bundle_bucket, bundle_object_key, candidate_model_version_id,
    run_digest, created_at, updated_at
"""


def _refuse(refusals: Sequence[Refusal]) -> None:
    raise RunRefused(tuple(refusals))


def _one(check_id: str, code: str, message: str, **kw: Any) -> Refusal:
    return Refusal(check_id=check_id, code=code, message=message, **kw)


# =====================================================================================
# The binding
# =====================================================================================
@dataclass(frozen=True)
class RunBinding:
    """`MOS-TRAIN-124`'s field table as one object. Frozen; every field required.

    There are no defaults on the nine reproducibility inputs and there MUST NOT be. A
    default on `seeds` would mean a run whose seeds nobody chose, recorded as though
    somebody had; a default on `hardware` would mean a `ValidationReport` naming a GPU the
    run did not use. `MOS-TRAIN-126`'s whole claim is that these are *recorded rather than
    asserted*, and a default is an assertion wearing a record's clothes.

    `fit_partition` and `select_partition` DO carry `MOS-TRAIN-124`'s stated defaults
    (`'train'` / `'tune'`), because those are the requirement's own defaults and neither
    can be spelled `test` -- 0013 constrains both columns and `medos.training.cohort`
    refuses the read.
    """

    capability_id: str
    dataset_version_id: str
    dataset_version_digest: str
    split_id: str
    split_digest: str
    annotation_set_id: str
    annotation_digest: str
    preprocessing_spec_id: str
    preprocessing_spec_version: int
    preprocessing_spec_digest: str
    code_commit: str
    code_dirty: bool
    image_digest: str
    training_backend: Mapping[str, Any]
    hyperparameters: Mapping[str, Any]
    seeds: Mapping[str, Any]
    determinism: Mapping[str, Any]
    hardware: Mapping[str, Any]
    framework_versions: Mapping[str, Any]
    fit_partition: str = "train"
    select_partition: str = "tune"
    backend_rationale: str | None = None
    search_id: str | None = None
    trial_index: int | None = None
    labels: Mapping[str, Any] = field(default_factory=dict)

    def as_digest_document(self) -> dict[str, Any]:
        """What `run_digest` is computed over. `MOS-TRAIN-124`: "the full binding above".

        `labels` is NOT in it: a label is an operator's note about a run, and a run whose
        digest changed because somebody re-tagged it would no longer be the same
        experiment by the only definition that matters.
        """
        return {
            "capability_id": self.capability_id,
            "dataset_version_digest": self.dataset_version_digest,
            "split_digest": self.split_digest,
            "fit_partition": self.fit_partition,
            "select_partition": self.select_partition,
            "annotation_digest": self.annotation_digest,
            "preprocessing_spec_id": self.preprocessing_spec_id,
            "preprocessing_spec_version": int(self.preprocessing_spec_version),
            "preprocessing_spec_digest": self.preprocessing_spec_digest,
            "code_commit": self.code_commit,
            "code_dirty": bool(self.code_dirty),
            "image_digest": self.image_digest,
            "training_backend": dict(self.training_backend),
            "hyperparameters": dict(self.hyperparameters),
            "seeds": dict(self.seeds),
            "determinism": dict(self.determinism),
            "hardware": dict(self.hardware),
            "framework_versions": dict(self.framework_versions),
            # A uuid has no canonical JSON form (`MOS-SEC-151`), and the digest is
            # over CONTENT rather than over Python types, so the id is a string here.
            "search_id": str(self.search_id) if self.search_id else None,
            "trial_index": self.trial_index,
        }


def hyperparameters_digest_of(hyperparameters: Mapping[str, Any]) -> str:
    """`sha256:` over the JCS form. `MOS-TRAIN-124`: "digested under JCS (MOS-EVID-008)"."""
    return "sha256:" + sha256_hex(canonical_bytes(dict(hyperparameters)))


def run_digest_of(binding: RunBinding) -> str:
    """`sha256:` over the canonical serialisation of the full binding. `MOS-TRAIN-124`."""
    return "sha256:" + sha256_hex(canonical_bytes(binding.as_digest_document()))


# =====================================================================================
# The row
# =====================================================================================
@dataclass(frozen=True)
class RunRow:
    """One `training_runs` row, as a mapping plus the accessors callers actually use."""

    row: Mapping[str, Any]

    @property
    def id(self) -> str:
        return str(self.row["id"])

    @property
    def public_id(self) -> str:
        return str(self.row["public_id"])

    @property
    def state(self) -> str:
        return str(self.row["state"])

    @property
    def capability_id(self) -> str:
        return str(self.row["capability_id"])

    @property
    def backend_kind(self) -> str:
        return str((self.row["training_backend"] or {}).get("kind", ""))

    @property
    def bundle_digest(self) -> str | None:
        value = self.row.get("bundle_digest")
        return str(value) if value else None

    @property
    def run_digest(self) -> str:
        return str(self.row["run_digest"])

    def __getitem__(self, key: str) -> Any:
        return self.row[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.row.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.row)


# =====================================================================================
# Submit
# =====================================================================================
def submit(
    conn: psycopg.Connection[Any],
    *,
    binding: RunBinding,
    actor: audit.Actor,
    trace_id: str,
    leakage: LeakageReport | Mapping[str, Any] | None = None,
    output_kind: str | None = None,
    request_id: str | None = None,
    tenant_id: str | None = None,
) -> RunRow:
    """Record one `TrainingRun` at `PENDING`. Refuses before anything expensive happens.

    `leakage` is the split's `leakage_report` -- either the `LeakageReport` object or the
    stored jsonb. It is REQUIRED in the sense that `None` means "this caller did not look",
    and a caller that did not look is refused: `MOS-TRAIN-115` puts the check "before the
    first batch is loaded", and an optional check with a permissive default is not a check.

    `output_kind` is the bound `PreprocessingSpec`'s `io.output_kind` (`MOS-IMG-049`), and
    it is what `MOS-TRAIN-211` keys on. When it is `None` this module reads the capability
    registry instead and treats a `SEG` output kind as `label`; when neither is available
    the rationale rule cannot be evaluated and the run is refused rather than admitted,
    because "nobody could tell" is not the same answer as "the default was compared".

    The row, the audit event and (when the run is a trial) nothing else commit in one
    transaction. `MOS-SEC-149`: a recorded act with no audit event is not a recorded act.
    """
    refusals: list[Refusal] = []
    refusals += _binding_refusals(binding)
    refusals += _leakage_refusals(binding, leakage)
    refusals += _backend_refusals(binding, output_kind)
    if refusals:
        _refuse(refusals)

    digest = run_digest_of(binding)
    public_id = new_ulid("tr")
    tenant = str(tenant_id or current_tenant())

    with tenant_tx(conn, tenant) as tx:
        row = tx.execute(
            f"""
            INSERT INTO training_runs (
                public_id, tenant_id, capability_id,
                dataset_version_id, dataset_version_digest, split_id, split_digest,
                fit_partition, select_partition, annotation_set_id, annotation_digest,
                preprocessing_spec_id, preprocessing_spec_version,
                preprocessing_spec_digest, code_commit, code_dirty, image_digest,
                training_backend, backend_rationale, hyperparameters,
                hyperparameters_digest, search_id, trial_index,
                seeds, determinism, hardware, framework_versions, run_digest)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_COLUMNS}
            """,
            (
                public_id,
                _uuid.UUID(tenant),
                binding.capability_id,
                _uuid.UUID(str(binding.dataset_version_id)),
                binding.dataset_version_digest,
                _uuid.UUID(str(binding.split_id)),
                binding.split_digest,
                binding.fit_partition,
                binding.select_partition,
                _uuid.UUID(str(binding.annotation_set_id)),
                binding.annotation_digest,
                binding.preprocessing_spec_id,
                int(binding.preprocessing_spec_version),
                binding.preprocessing_spec_digest,
                binding.code_commit,
                bool(binding.code_dirty),
                binding.image_digest,
                Jsonb(dict(binding.training_backend)),
                binding.backend_rationale,
                Jsonb(dict(binding.hyperparameters)),
                hyperparameters_digest_of(binding.hyperparameters),
                _uuid.UUID(str(binding.search_id)) if binding.search_id else None,
                binding.trial_index,
                Jsonb(dict(binding.seeds)),
                Jsonb(dict(binding.determinism)),
                Jsonb(dict(binding.hardware)),
                Jsonb(dict(binding.framework_versions)),
                digest,
            ),
        ).fetchone()

        audit.record(
            conn,
            action="training.run.submit",
            action_class="governance",
            actor=actor,
            resource=audit.Resource(kind="training_run", id=public_id),
            outcome="allow",
            pep="api.request",
            trace_id=trace_id,
            request_id=request_id or audit.new_request_id(),
            detail={
                "capability_id": binding.capability_id,
                "split_digest": binding.split_digest,
                "run_digest": digest,
                "backend": dict(binding.training_backend).get("kind"),
                "code_dirty": bool(binding.code_dirty),
            },
        )
    return RunRow(dict(row))


def _binding_refusals(binding: RunBinding) -> list[Refusal]:
    """`MOS-TRAIN-124`: every input pinned, in the shape the requirement writes."""
    out: list[Refusal] = []

    if binding.fit_partition != "train" or binding.select_partition not in ("train", "tune"):
        out.append(
            _one(
                "MOS-TRAIN-141",
                "partition_not_readable_by_pipeline",
                "fit_partition MUST be 'train' and select_partition MUST be 'train' or "
                "'tune'; the training and selection jobs MUST NOT be able to read the "
                "test partition at all",
                observed=[binding.fit_partition, binding.select_partition],
                bound=["train", "tune"],
            )
        )

    if not _COMMIT_RE.match(binding.code_commit or ""):
        out.append(
            _one(
                "MOS-TRAIN-124",
                "code_commit_not_a_revision",
                "code_commit MUST be a 40- or 64-character lower-case hex object name",
                observed=binding.code_commit,
            )
        )

    for name, value in (
        ("dataset_version_digest", binding.dataset_version_digest),
        ("split_digest", binding.split_digest),
        ("annotation_digest", binding.annotation_digest),
        ("preprocessing_spec_digest", binding.preprocessing_spec_digest),
        ("image_digest", binding.image_digest),
    ):
        if not _DIGEST_RE.match(str(value or "")):
            out.append(
                _one(
                    "MOS-TRAIN-124",
                    "digest_not_pinned",
                    f"{name} MUST be 'sha256:' followed by 64 lower-case hex characters",
                    observed=value,
                    detail={"field": name},
                )
            )

    for name, keys, block in (
        ("seeds", _SEED_KEYS, binding.seeds),
        ("determinism", _DETERMINISM_KEYS, binding.determinism),
        ("hardware", _HARDWARE_KEYS, binding.hardware),
        ("framework_versions", _FRAMEWORK_KEYS, binding.framework_versions),
    ):
        missing = [k for k in keys if k not in (block or {})]
        if missing:
            out.append(
                _one(
                    "MOS-TRAIN-124",
                    "reproducibility_block_incomplete",
                    f"{name} is missing {missing}. MOS-TRAIN-126: what the record "
                    "guarantees is that 'every input is pinned, the determinism settings "
                    "actually used are recorded rather than asserted'. An absent key is "
                    "not an open value; it is a question nobody answered",
                    observed=sorted((block or {}).keys()),
                    bound=list(keys),
                    detail={"field": name, "missing": missing},
                )
            )

    kind = dict(binding.training_backend or {}).get("kind")
    if kind not in BACKEND_KINDS:
        out.append(
            _one(
                "MOS-TRAIN-124",
                "unknown_training_backend",
                f"training_backend.kind MUST be one of {BACKEND_KINDS}",
                observed=kind,
                bound=list(BACKEND_KINDS),
            )
        )
    if not str(dict(binding.training_backend or {}).get("version") or ""):
        out.append(
            _one(
                "MOS-TRAIN-124",
                "training_backend_version_not_pinned",
                "training_backend.version MUST be an exact pin; MOS-REL-037 forbids "
                "expressing compatibility against a product version range",
                observed=dict(binding.training_backend or {}).get("version"),
            )
        )

    if (binding.search_id is None) != (binding.trial_index is None):
        out.append(
            _one(
                "MOS-TRAIN-219",
                "trial_binding_incomplete",
                "a trial carries search_id AND trial_index; MOS-TRAIN-219 forbids a "
                "second, lighter run record for trials",
                observed={"search_id": binding.search_id,
                          "trial_index": binding.trial_index},
            )
        )
    return out


def _leakage_refusals(
    binding: RunBinding, leakage: LeakageReport | Mapping[str, Any] | None
) -> list[Refusal]:
    """`MOS-TRAIN-115`/`MOS-TRAIN-116`: the leakage check BLOCKS, and a waiver does not lift it.

    "Assert the freeze-time L1 of `MOS-EVID-034` reports `fail`; then attach a
    `MOS-EVID-036` waiver to it and submit a `TrainingRun` against that split. Assert the
    run is refused before the first batch is loaded, with the violating `patient_key` and
    both partitions named, and that the waiver did not permit it."

    The verdict comes from `medos.evidence.leakage.waiver_blocks_training`, which is
    chapter 7's own function. Reimplementing the rule here would give the platform two
    answers to "may this cohort be trained on", and the one that wins would be whichever
    call site a reviewer happened to read.
    """
    if leakage is None:
        return [
            _one(
                "MOS-TRAIN-115",
                "leakage_report_not_supplied",
                "a TrainingRun MUST be checked against the split's leakage report before "
                "the first batch is loaded; submitting without one is not the same as "
                "submitting against a clean one",
                observed=None,
                bound="the split's leakage_report",
                detail={"split_digest": binding.split_digest},
            )
        ]

    report = leakage if isinstance(leakage, LeakageReport) else _report_from_mapping(leakage)
    blocking = waiver_blocks_training(report)
    if not blocking:
        return []

    hits: list[dict[str, Any]] = []
    for check in report.checks:
        if check.id in blocking:
            hits.extend(dict(h) for h in check.hits[:20])
    return [
        _one(
            "MOS-TRAIN-115",
            "leakage_blocks_training",
            f"{', '.join(blocking)} is fail or waived on this split. MOS-TRAIN-116: a "
            "waiver permits a REPORT to be written about the cohort; it does not permit "
            "a model to be fitted on it. The remedy is a new split, or -- for an identity "
            "defect -- a patient_key_aliases row and a re-seal (MOS-TRAIN-118)",
            observed=list(blocking),
            bound=[],
            detail={"split_digest": binding.split_digest, "hits": hits},
        )
    ]


def _report_from_mapping(stored: Mapping[str, Any]) -> LeakageReport:
    """Rebuild a `LeakageReport` from the jsonb `dataset_splits.leakage_report` holds."""
    from medos.evidence.leakage import LeakageCheck

    checks: list[LeakageCheck] = []
    for cid in ("L1", "L2", "L3", "L4", "L5"):
        raw = stored.get(cid)
        if raw is None:
            continue
        if isinstance(raw, str):
            checks.append(LeakageCheck(id=cid, outcome=raw))
        elif isinstance(raw, Mapping):
            checks.append(
                LeakageCheck(
                    id=cid,
                    outcome=str(raw.get("outcome", "skipped")),
                    hits=tuple(dict(h) for h in raw.get("hits", ())),
                    detail=dict(raw.get("detail") or {}) or None,
                )
            )
    return LeakageReport(checks=tuple(checks))


def _backend_refusals(binding: RunBinding, output_kind: str | None) -> list[Refusal]:
    """`MOS-TRAIN-211`: the default for a `label` capability, and what a deviation owes.

    "For a capability whose `io.output_kind` is `label` (`MOS-IMG-049`), the default
    `training_backend.kind` MUST be a dataset-fingerprint auto-configuring backend --
    `nnunet` or `auto3dseg`. A hand-configured `monai_supervised` run for such a capability
    MAY be submitted and MUST record a `backend_rationale` of at least 20 characters ...
    naming what the auto-configured baseline failed to do."
    """
    kind = dict(binding.training_backend or {}).get("kind")
    if kind != "monai_supervised":
        if binding.backend_rationale is not None:
            return [
                _one(
                    "MOS-TRAIN-211",
                    "rationale_without_a_deviation",
                    "backend_rationale names what the auto-configured baseline failed to "
                    "do; an auto-configuring run has no baseline to have deviated from",
                    observed=kind,
                )
            ]
        return []

    resolved = output_kind or _output_kind_of(binding.capability_id)
    if resolved is None:
        return [
            _one(
                "MOS-TRAIN-211",
                "output_kind_unknown",
                f"capability {binding.capability_id!r} has no io.output_kind this "
                "process can read, so whether MOS-TRAIN-211's default applies cannot be "
                "decided. Pass output_kind= from the bound PreprocessingSpec. 'Nobody "
                "could tell' is not the same answer as 'the default was compared'",
                observed=binding.capability_id,
            )
        ]
    if resolved != "label":
        return []
    if not binding.backend_rationale or len(binding.backend_rationale) < 20:
        return [
            _one(
                "MOS-TRAIN-211",
                "hand_configured_backend_without_rationale",
                "a hand-configured monai_supervised run for a label capability MUST "
                "record a backend_rationale of at least 20 characters naming what the "
                "auto-configured baseline failed to do. The rationale is not a gate; it "
                "exists so that the first question at the approval gate is answerable: "
                "was this compared against the default, or did nobody run the default?",
                observed=len(binding.backend_rationale or ""),
                bound=20,
                detail={"capability_id": binding.capability_id,
                        "output_kind": resolved},
            )
        ]
    return []


def _output_kind_of(capability_id: str) -> str | None:
    """`io.output_kind` for a capability this deployment ships, or `None`.

    CONTRACT.md section 6 makes `medos.capabilities.REGISTRY` the meaning of a capability
    id in this tree, and chapter 9's metadata block carries `output_kinds` -- a `SEG`
    output is `MOS-IMG-049`'s `label`. When section 12.9.1's `capabilities` catalogue
    lands, this function is where it is read from instead.
    """
    try:
        from medos.capabilities import metadata_for
    except ImportError:  # pragma: no cover - the package always ships
        return None
    try:
        meta = metadata_for(capability_id)
    except KeyError:
        return None
    if "SEG" in meta.output_kinds:
        return "label"
    if "MEASUREMENT" in meta.output_kinds or "SR" in meta.output_kinds:
        return "probability"
    return None


# =====================================================================================
# Lifecycle
# =====================================================================================
def start(
    conn: psycopg.Connection[Any],
    *,
    run_id: str,
    runner: str,
    orchestrator_run_id: str | None = None,
    training_backend: Mapping[str, Any] | None = None,
    fingerprint_digest: str | None = None,
    fingerprint_location: tuple[str, str] | None = None,
    now: datetime | None = None,
    tenant_id: str | None = None,
) -> RunRow:
    """`PENDING -> RUNNING`, and the one moment the derived plan may be written.

    `MOS-TRAIN-135`: "Its self-configured plan MUST be frozen at run start and recorded as
    `training_backend.plan_digest`." `MOS-TRAIN-223`: the fingerprint document itself is
    digested as `fingerprint_digest` and retained for audit. After this call 0013's
    `training_runs_guard()` refuses any further change to either, which is what makes
    `MOS-TRAIN-225`'s freeze verifiable: a re-derivation at serve time produces a
    materially different transform "executed by a byte-identical container, against a
    byte-identical weights digest, with every structural check passing".
    """
    run = get(conn, run_id, tenant_id=tenant_id)
    if run.state != "PENDING":
        raise TrainingError(
            f"training run {run.public_id} is {run.state}, not PENDING; the derived "
            "configuration is frozen at run start (MOS-TRAIN-135)"
        )

    kind = run.backend_kind
    if kind in AUTO_CONFIGURING_BACKENDS and not fingerprint_digest:
        _refuse(
            [
                _one(
                    "MOS-TRAIN-223",
                    "fingerprint_not_frozen",
                    f"a {kind} run MUST freeze its derived configuration at run start "
                    "and record the fingerprint document's digest. MOS-TRAIN-225: the "
                    "serving path MUST NOT re-derive any fingerprint quantity, and the "
                    "freeze is what makes that checkable",
                    observed=None,
                    detail={"backend": kind},
                )
            ]
        )
    if kind == "monai_supervised" and fingerprint_digest:
        _refuse(
            [
                _one(
                    "MOS-TRAIN-223",
                    "fingerprint_without_derivation",
                    "a hand-configured run derives nothing from the cohort, so it has no "
                    "fingerprint document to digest",
                    observed=fingerprint_digest,
                )
            ]
        )

    backend = dict(training_backend) if training_backend is not None else dict(
        run["training_backend"] or {}
    )
    if kind in AUTO_CONFIGURING_BACKENDS and not backend.get("plan_digest"):
        backend["plan_digest"] = fingerprint_digest
    if backend.get("kind") != kind:
        raise TrainingError(
            "training_backend.kind is part of the sealed binding and MUST NOT change at "
            "run start (MOS-TRAIN-124)"
        )

    bucket, key = fingerprint_location or (None, None)
    stamp = now or datetime.now(UTC)
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            f"""
            UPDATE training_runs
               SET state = 'RUNNING', started_at = %s, runner = %s,
                   orchestrator_run_id = %s, training_backend = %s,
                   fingerprint_digest = %s, fingerprint_bucket = %s,
                   fingerprint_object_key = %s
             WHERE id = %s AND state = 'PENDING'
            RETURNING {_COLUMNS}
            """,
            (stamp, runner, orchestrator_run_id, Jsonb(backend), fingerprint_digest,
             bucket, key, _uuid.UUID(run.id)),
        ).fetchone()
    if row is None:  # pragma: no cover - the guard above already read the state
        raise TrainingError(f"training run {run_id} was not PENDING")
    return RunRow(dict(row))


def succeed(
    conn: psycopg.Connection[Any],
    *,
    run_id: str,
    bundle_digest: str,
    bundle_location: tuple[str, str] | None = None,
    now: datetime | None = None,
    tenant_id: str | None = None,
) -> RunRow:
    """`RUNNING -> SUCCEEDED`. `MOS-TRAIN-129`: the output artifact is a MONAI Bundle.

    Refuses a dirty tree here rather than at submit, because that is where
    `MOS-TRAIN-125` puts it: "`code_dirty = true` MUST block `state = SUCCEEDED`". 0013
    carries the same rule as a CHECK; this is the message that names the reason.
    """
    run = get(conn, run_id, tenant_id=tenant_id)
    if run.state != "RUNNING":
        raise TrainingError(f"training run {run.public_id} is {run.state}, not RUNNING")
    if bool(run["code_dirty"]):
        _refuse(
            [
                _one(
                    "MOS-TRAIN-125",
                    "dirty_tree_cannot_succeed",
                    "a run from an uncommitted tree cannot be re-entered, and a candidate "
                    "that cannot be re-entered cannot be diagnosed when it later fails in "
                    "a subgroup nobody looked at. Commit, and submit a new run",
                    observed=True,
                    bound=False,
                    detail={"code_commit": run["code_commit"]},
                )
            ]
        )
    if not _DIGEST_RE.match(bundle_digest or ""):
        _refuse(
            [
                _one(
                    "MOS-TRAIN-129",
                    "bundle_digest_not_pinned",
                    "a SUCCEEDED run's output artifact MUST be a MONAI Bundle with one "
                    "content digest",
                    observed=bundle_digest,
                )
            ]
        )

    bucket, key = bundle_location or (None, None)
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            f"""
            UPDATE training_runs
               SET state = 'SUCCEEDED', finished_at = %s, bundle_digest = %s,
                   bundle_bucket = %s, bundle_object_key = %s
             WHERE id = %s AND state = 'RUNNING'
            RETURNING {_COLUMNS}
            """,
            (now or datetime.now(UTC), bundle_digest, bucket, key, _uuid.UUID(run.id)),
        ).fetchone()
    if row is None:  # pragma: no cover
        raise TrainingError(f"training run {run_id} was not RUNNING")
    return RunRow(dict(row))


def fail(
    conn: psycopg.Connection[Any],
    *,
    run_id: str,
    reason: str,
    now: datetime | None = None,
    tenant_id: str | None = None,
) -> RunRow:
    """Any non-terminal state -> `FAILED`, with a reason. `MOS-REL-051`: never silent."""
    if not reason:
        raise ValueError("a FAILED training run MUST name a reason (MOS-REL-051)")
    return _terminate(conn, run_id, "FAILED", reason, now, tenant_id)


def cancel(
    conn: psycopg.Connection[Any],
    *,
    run_id: str,
    now: datetime | None = None,
    tenant_id: str | None = None,
) -> RunRow:
    """Any non-terminal state -> `CANCELLED`. The operator's verb, not the pipeline's."""
    return _terminate(conn, run_id, "CANCELLED", None, now, tenant_id)


def _terminate(
    conn: psycopg.Connection[Any],
    run_id: str,
    state: str,
    reason: str | None,
    now: datetime | None,
    tenant_id: str | None,
) -> RunRow:
    run = get(conn, run_id, tenant_id=tenant_id)
    if run.state in ("SUCCEEDED", "FAILED", "CANCELLED"):
        raise TrainingError(
            f"training run {run.public_id} is already terminal ({run.state}); a terminal "
            "run is not reopened"
        )
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            f"""
            UPDATE training_runs
               SET state = %s, failure_reason = %s, finished_at = %s,
                   started_at = coalesce(started_at, %s)
             WHERE id = %s AND state IN ('PENDING','RUNNING')
            RETURNING {_COLUMNS}
            """,
            (state, reason, now or datetime.now(UTC), now or datetime.now(UTC),
             _uuid.UUID(run.id)),
        ).fetchone()
    if row is None:  # pragma: no cover
        raise TrainingError(f"training run {run_id} was already terminal")
    return RunRow(dict(row))


# =====================================================================================
# Reads
# =====================================================================================
_UUID_RE: Final[re.Pattern[str]] = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def get(
    conn: psycopg.Connection[Any], ident: str, *, tenant_id: str | None = None
) -> RunRow:
    """One run by `public_id` or uuid, within the tenant's visibility."""
    column = "id::text" if _UUID_RE.match(ident) else "public_id"
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            f"SELECT {_COLUMNS} FROM training_runs WHERE {column} = %s", (ident,)
        ).fetchone()
    if row is None:
        raise TrainingError(f"no training run {ident}")
    return RunRow(dict(row))


def list_runs(
    conn: psycopg.Connection[Any],
    *,
    capability_id: str | None = None,
    split_digest: str | None = None,
    search_id: str | None = None,
    state_in: Sequence[str] | None = None,
    limit: int = 100,
    tenant_id: str | None = None,
) -> tuple[RunRow, ...]:
    """Runs, newest first. No free-text filter: every argument is a bound column."""
    clauses: list[str] = []
    params: list[Any] = []
    if capability_id:
        clauses.append("capability_id = %s")
        params.append(capability_id)
    if split_digest:
        clauses.append("split_digest = %s")
        params.append(split_digest)
    if search_id:
        clauses.append("search_id = %s")
        params.append(_uuid.UUID(str(search_id)))
    if state_in:
        clauses.append("state = ANY(%s)")
        params.append(list(state_in))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    with tenant_tx(conn, tenant_id) as tx:
        rows = tx.execute(
            f"SELECT {_COLUMNS} FROM training_runs{where} "
            f"ORDER BY created_at DESC, public_id DESC LIMIT %s",
            tuple(params),
        ).fetchall()
    return tuple(RunRow(dict(r)) for r in rows)


# =====================================================================================
# MOS-TRAIN-127 -- the seed-variance characterisation
# =====================================================================================
def record_seed_variance(
    conn: psycopg.Connection[Any],
    *,
    capability_id: str,
    metric: str,
    sd: float,
    training_run_ids: Sequence[str],
    backend_kind: str,
    gpu_count: int,
    hyperparameters_digest: str,
    recorded_by: str,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """`{"metric": ..., "runs": n, "sd": ...}` for a capability. `MOS-TRAIN-127`.

    "at least three `TrainingRun`s differing only in `seeds`, each evaluated on the same
    `test` partition, with the observed standard deviation of the primary metric
    persisted."

    The "differing only in seeds" half is checked here and not left to the caller: the
    three runs' bindings are compared with their `seeds` removed, and a set that differs in
    anything else is refused. A characterisation assembled from runs that also differ in
    hyperparameters measures the hyperparameters.
    """
    ids = [str(x) for x in training_run_ids]
    if len(set(ids)) < 3:
        _refuse(
            [
                _one(
                    "MOS-TRAIN-127",
                    "seed_variance_needs_three_runs",
                    "at least three TrainingRuns differing only in seeds",
                    observed=len(set(ids)),
                    bound=3,
                )
            ]
        )

    runs = [get(conn, i, tenant_id=tenant_id) for i in ids]
    shapes = set()
    for run in runs:
        doc = dict(run.row)
        shapes.add(
            sha256_hex(
                canonical_bytes(
                    {
                        "capability_id": doc["capability_id"],
                        "dataset_version_digest": doc["dataset_version_digest"],
                        "split_digest": doc["split_digest"],
                        "annotation_digest": doc["annotation_digest"],
                        "preprocessing_spec_digest": doc["preprocessing_spec_digest"],
                        "image_digest": doc["image_digest"],
                        "training_backend": doc["training_backend"],
                        "hyperparameters_digest": doc["hyperparameters_digest"],
                    }
                )
            )
        )
    if len(shapes) != 1:
        _refuse(
            [
                _one(
                    "MOS-TRAIN-127",
                    "seed_variance_runs_differ_in_more_than_seeds",
                    "the runs differ in something other than `seeds`, so their spread "
                    "measures that something rather than run-to-run noise",
                    observed=len(shapes),
                    bound=1,
                    detail={"training_run_ids": ids},
                )
            ]
        )

    tenant = str(tenant_id or current_tenant())
    with tenant_tx(conn, tenant) as tx:
        tx.execute(
            "UPDATE capability_seed_variance SET superseded_at = now() "
            "WHERE capability_id = %s AND superseded_at IS NULL",
            (capability_id,),
        )
        row = tx.execute(
            """
            INSERT INTO capability_seed_variance
                (tenant_id, capability_id, metric, runs, sd, training_run_ids,
                 backend_kind, gpu_count, hyperparameters_digest, recorded_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id, capability_id, metric, runs, sd, backend_kind, gpu_count,
                      recorded_at
            """,
            (
                _uuid.UUID(tenant), capability_id, metric, len(set(ids)), float(sd),
                [_uuid.UUID(str(get(conn, i, tenant_id=tenant_id).id)) for i in ids],
                backend_kind, int(gpu_count), hyperparameters_digest,
                _uuid.UUID(str(recorded_by)),
            ),
        ).fetchone()
    return dict(row)


def seed_variance(
    conn: psycopg.Connection[Any], *, capability_id: str, tenant_id: str | None = None
) -> dict[str, Any] | None:
    """The live characterisation, in `MOS-TRAIN-127`'s own shape, or `None`."""
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            "SELECT metric, runs, sd, backend_kind, gpu_count, recorded_at "
            "FROM capability_seed_variance "
            "WHERE capability_id = %s AND superseded_at IS NULL",
            (capability_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    return {
        "metric": d["metric"],
        "runs": int(d["runs"]),
        "sd": float(d["sd"]),
        "backend_kind": d["backend_kind"],
        "gpu_count": int(d["gpu_count"]),
        "recorded_at": d["recorded_at"],
    }


# =====================================================================================
# MOS-TRAIN-216 -- the test partition as a consumable
# =====================================================================================
def note_test_exposure(
    conn: psycopg.Connection[Any],
    *,
    capability_id: str,
    split_digest: str,
    now: datetime | None = None,
    tenant_id: str | None = None,
) -> int:
    """Increment `test_exposure_count` for `(capability_id, split_digest)`. Returns it.

    `MOS-TRAIN-216`: "MUST increment it on every such run, MUST render it in the approval
    dossier, and MUST NOT allow it to be reset. A split's `test` partition is a
    consumable; the counter is how a team finds out it has been spent."

    Called once per distinct `SUCCEEDED` `EvaluationRun` on the split's `test` partition.
    There is no decrement and no reset: 0013 revokes DELETE and the monotonicity trigger
    binds the owner too.
    """
    stamp = now or datetime.now(UTC)
    tenant = str(tenant_id or current_tenant())
    with tenant_tx(conn, tenant) as tx:
        row = tx.execute(
            """
            INSERT INTO split_test_exposure
                (tenant_id, capability_id, split_digest, exposure_count,
                 first_exposed_at, last_exposed_at)
            VALUES (%s, %s, %s, 1, %s, %s)
            ON CONFLICT (tenant_id, capability_id, split_digest) DO UPDATE
               SET exposure_count = split_test_exposure.exposure_count + 1,
                   last_exposed_at = EXCLUDED.last_exposed_at
            RETURNING exposure_count
            """,
            (_uuid.UUID(tenant), capability_id, split_digest, stamp, stamp),
        ).fetchone()
    return int(dict(row)["exposure_count"])


def test_exposure_count(
    conn: psycopg.Connection[Any],
    *,
    capability_id: str,
    split_digest: str,
    tenant_id: str | None = None,
) -> int:
    """How many times this split's `test` partition has been read. `MOS-TRAIN-216`."""
    with tenant_tx(conn, tenant_id) as tx:
        row = tx.execute(
            "SELECT exposure_count FROM split_test_exposure "
            "WHERE capability_id = %s AND split_digest = %s",
            (capability_id, split_digest),
        ).fetchone()
    return int(dict(row)["exposure_count"]) if row else 0
