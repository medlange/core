# SPDX-License-Identifier: Apache-2.0
"""THE single in-repo manifest schema source. `MOS-REG-015`, `MOS-REG-016`.

    `MOS-REG-016` -- "All manifest schemas MUST be generated from a single in-repo schema
    source ... A hand-maintained duplicate of any manifest shape is forbidden."

So: this module, and nothing else, states the shape of an artifact manifest. The rows of
`artifact_manifest_schemas` are GENERATED from it (`schema_rows()`, and `sql_seed()` which
`0012_artifacts.up.sql` carries), and
`tests/integration/test_artifacts.py::test_seeded_schemas_are_the_in_repo_source` asserts
the applied database and this file agree digest-for-digest, so the seed cannot drift.

WHICH KINDS HAVE A SCHEMA, AND WHY THE OTHERS DO NOT
-----------------------------------------------------
Chapter 6 writes exactly two manifest schemas -- `service_version` (§6.4) and
`model_version` (§6.5) -- and those two are here, complete. The other five kinds exist in
the table's CHECK constraints and in `MOS-REG-020`'s status table, so the column can carry
them, but 0012 seeds no schema for them and the foreign key
`artifacts (kind, manifest_schema_version) -> artifact_manifest_schemas` therefore REFUSES
the row. That refusal is the honest state of each one:

  `preprocessing_spec`  Chapter 4 owns the field list (`MOS-IMG-049`, "the schema below is
                        complete") and `medos/medos/training/spec.py` already implements it as a
                        registration validator. Restating those forty-five fields here
                        would be exactly the hand-maintained duplicate `MOS-REG-016`
                        forbids. Owed: generate both from one source.
  `dataset_version`,    Chapter 7 originates and seals these (`MOS-EVID-014`; the registry
  `annotation_set`      "stores but never originates" `SEALED`/`DEFECTIVE`), and 0006 gave
                        them their own `dataset_versions` / `annotation_sets` tables. No
                        chapter writes their manifest shape. Owed, and reported.
  `policy_set_version`  `MOS-REG-112` requires it; Chapter 8 §8.8 owns the bundle content,
                        and `policy_activations` -- the table whose `policy_set_digest`
                        MUST resolve to the artifact row -- does not exist in this schema
                        yet. A schema invented here would be a guess at another chapter's
                        document.
  `workflow_version`    §15.2.6: "No `Workflow`, `WorkflowVersion`, `Tool`, `ToolVersion`,
                        `Agent` or `AgentVersion` is backed by a table, endpoint or
                        executor before 0.4.0." Absence of the schema row makes that
                        prohibition a foreign-key failure rather than a code review.

§15.1.3 names "`Artifact` registry breadth" as the degradable item of this release; this is
that degradation, stated per kind with its reason, rather than a silent gap.

THE GOLDEN-FIXTURE DIGEST ENCODING. A LIVE SPEC CONTRADICTION, RESOLVED THE SAME WAY TWICE
-------------------------------------------------------------------------------------------
`MOS-REG-036` says every member of `golden_fixture` is "a **bare lowercase 64-character
hex string**, because Chapter 4 owns those fields and types them so (`MOS-IMG-049`)", and
that a validator "MUST reject" the `sha256:` prefix. `MOS-IMG-049` says the opposite in
terms: "Every field typed `sha256 digest` above is encoded as `"sha256:"` followed by 64
lower-case hex characters ... A bare hex string is invalid."
`docs/spec/99-known-inconsistencies.md` entry 2 records the clash and resolves it for
chapter 4: keep the prefix, correct `MOS-REG-036`. `medos/medos/training/spec.py` had already
made the same call for the same reason. This module follows chapter 4 and the register, so
the one platform has one encoding -- and states plainly that chapter 6's own example
manifest at `06-registries.md:320` is therefore refused by this schema. REPORTED, not
silently patched; see this component's report.

Spec: MOS-REG-013, MOS-REG-014, MOS-REG-015, MOS-REG-016, MOS-REG-017, MOS-REG-024,
MOS-REG-027, MOS-REG-032, MOS-REG-033, MOS-REG-036, MOS-REG-037, MOS-REG-039,
MOS-REG-095, MOS-REG-097, MOS-REG-098, MOS-REG-104, MOS-REG-113, MOS-IMG-049,
MOS-STORE-253. Pure: no I/O, no clock, no database.
"""

from __future__ import annotations

import json
from typing import Any, Final

from medos.registry.jsonschema import (
    SchemaError,
    assert_closed_objects,
    check_schema,
    iter_errors,
)
from medos.registry.lifecycle import FAMILY_KIND, VERSION_KIND
from medos.sdk.canonical import canonical_bytes, sha256_hex

__all__ = [
    "BASE_URI",
    "ENVELOPE_SCHEMA",
    "INTRODUCED_IN",
    "SCHEMAS",
    "SCHEMA_VERSION",
    "SEEDED_KINDS",
    "manifest_errors",
    "schema_for_kind",
    "schema_id_for_kind",
    "schema_rows",
    "sql_seed",
]

# `MOS-REG-015`: "published at `https://schemas.medicalos.org/artifact/<kind>/
# <schema_version>.json`, versioned independently per kind". The URI is an identifier;
# nothing dereferences it at validation time (see `jsonschema._resolve`).
BASE_URI: Final[str] = "https://schemas.medicalos.org/artifact/"
SCHEMA_VERSION: Final[str] = "1.0.0"
INTRODUCED_IN: Final[str] = "0.3.0"

# The kinds 0012 seeds, family-level (`MOS-STORE-253`). See the module docstring for the
# five it does not and why each one is absent.
SEEDED_KINDS: Final[tuple[str, ...]] = ("service", "model")

# ------------------------------------------------------------------ shared patterns ---
# One statement each. A second copy of a digest pattern is how two call sites end up
# disagreeing about whether a prefix is allowed, which is the defect this file's docstring
# is about.
_SHA256 = r"^sha256:[0-9a-f]{64}$"
_SEMVER = r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z.-]+)?$"
# `capabilities.slug` of §12.9.1, which mirrors `MOS-REG-041`. Not re-spelled anywhere.
_CAPABILITY = r"^[a-z][a-z0-9_]{2,47}$"
# The publisher-chosen artifact id that `spec.models[].ref` and friends resolve against:
# chapter 6's own examples are `mv_pulmo_effusion_unet_3_2_1`, `ps_pulmo_effusion_prep_2_0_0`
# and `sv_01JQ8Z3K2M`, so the pattern admits both the slug form and the ULID form.
_ARTIFACT_REF = r"^(sv|mv|ps|dv|as|wv|pv)_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$"
# `MOS-REG-097`: exact pins for anything a serialized artifact is compiled against.
# A range operator in one of these fields is what `MOS-REG-098` rejects.
_EXACT_VERSION = r"^[0-9]+\.[0-9]+(\.[0-9]+)?$"
_GPU_ARCH = r"^sm_[0-9]{2,3}$"
_OCI_REF = r"^[a-z0-9][a-z0-9._/-]{1,254}$"


def _string(pattern: str | None = None, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "string"}
    if pattern:
        out["pattern"] = pattern
    out.update(extra)
    return out


def _obj(
    properties: dict[str, Any],
    required: list[str],
    *,
    nullable: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    """A CLOSED object schema. `additionalProperties: false` is not optional here."""
    out: dict[str, Any] = {
        "type": ["object", "null"] if nullable else "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }
    out.update(extra)
    return out


# =====================================================================================
# ServiceVersion -- chapter 6 section 6.4
# =====================================================================================
SERVICE_VERSION_SPEC: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"{BASE_URI}service_version/{SCHEMA_VERSION}.json",
    "title": "MedicalOS ServiceVersion manifest spec block",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "mode", "image", "capabilities", "modalities", "models", "resources",
        "legal_manufacturer", "regulatory_status", "compatibility",
    ],
    "properties": {
        # `MOS-SVC` owns the runtime meaning of `mode`; §6.7's filter F3 and the sealed
        # rule `MOS-REG-026` are what the registry does with it.
        "mode": {"enum": ["native", "sealed"]},
        # `MOS-REG-024`: a resolved sha256 digest. "A tag-only reference MUST be
        # rejected. The registry MUST NOT resolve a tag on the publisher's behalf" --
        # resolving it here would mean the thing that was signed and the thing that runs
        # are decided at different moments by different parties.
        "image": _obj(
            {"ref": _string(_OCI_REF), "digest": _string(_SHA256)},
            ["ref", "digest"],
        ),
        "capabilities": {
            "type": "array",
            "minItems": 1,
            "items": _obj(
                {
                    "id": _string(_CAPABILITY),
                    "outputs": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {
                            "enum": ["segmentation", "detection", "measurement",
                                     "classification"]
                        },
                    },
                    # `MOS-REG-113`: the operating threshold has exactly ONE member name
                    # across the platform and it is `score_threshold` (`MOS-SVC-020`).
                    "operating_points": {
                        "type": "array",
                        "minItems": 1,
                        "items": _obj(
                            {
                                "id": _string(r"^[a-z][a-z0-9_]{2,63}$"),
                                "score_threshold": {"type": "number", "minimum": 0.0,
                                                    "maximum": 1.0},
                            },
                            ["id", "score_threshold"],
                        ),
                    },
                },
                ["id", "outputs"],
            ),
        },
        "modalities": {
            "type": "array", "minItems": 1, "uniqueItems": True,
            "items": _string(r"^[A-Z]{2,16}$"),
        },
        "series_selector_ref": _string(r"^sel_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$"),
        # `MOS-REG-025` (native: non-empty, every ref resolves, VALIDATED/APPROVED) and
        # `MOS-REG-026` (sealed: exactly one `primary`) are cross-ROW rules and live in
        # `repo.publish`; the schema carries the shape and the floor.
        "models": {
            "type": "array",
            "minItems": 1,
            "items": _obj(
                {"ref": _string(_ARTIFACT_REF), "role": {"enum": ["primary", "dependency"]}},
                ["ref", "role"],
            ),
        },
        "preprocessing_specs": {
            "type": "array",
            "items": _obj({"ref": _string(_ARTIFACT_REF)}, ["ref"]),
        },
        "resources": _obj(
            {
                "gpu_required": {"type": "boolean"},
                "gpu_memory_mib": {"type": "integer", "minimum": 0},
                "cpu_millicores": {"type": "integer", "minimum": 1},
                "memory_mib": {"type": "integer", "minimum": 1},
                "max_concurrent_jobs": {"type": "integer", "minimum": 1},
            },
            ["gpu_required", "cpu_millicores", "memory_mib", "max_concurrent_jobs"],
        ),
        "engineering_acceptance": _obj(
            {
                "p95_wall_clock_seconds": {"type": "number", "exclusiveMinimum": 0},
                "max_gpu_memory_mib": {"type": "integer", "minimum": 0},
                "result_bundle_schema_validity": {"type": "number", "minimum": 0.0,
                                                  "maximum": 1.0},
            },
            ["p95_wall_clock_seconds", "result_bundle_schema_validity"],
        ),
        # `MOS-REG-027`: all four present, because these are the source of the DICOM
        # Enhanced General Equipment Type 1 attributes (chapter 4). A service version
        # missing one of them cannot produce a conformant SEG, so it is refused at
        # publish rather than at the writer.
        "legal_manufacturer": _obj(
            {
                "id": _string(r"^lm_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$"),
                "name": _string(minLength=1, maxLength=64),
                "device_serial_number": _string(minLength=1, maxLength=64),
                "software_versions": _string(minLength=1, maxLength=64),
            },
            ["id", "name", "device_serial_number", "software_versions"],
        ),
        "regulatory_status": {
            "type": "array",
            "minItems": 1,
            "items": _obj(
                {
                    "jurisdiction": _string(r"^[A-Z]{2}$"),
                    "status": {
                        "enum": ["not_a_medical_device", "not_cleared", "cleared",
                                 "ce_marked", "investigational"]
                    },
                    "evidence_ref": {"type": ["string", "null"]},
                },
                ["jurisdiction", "status", "evidence_ref"],
            ),
        },
        # §6.10.1 `MOS-REG-097`: ranges for software, exact pins for anything a
        # serialized artifact is compiled against.
        "compatibility": _obj(
            {
                "medicalos_api": _string(minLength=1, maxLength=64),
                "service_contract": _string(minLength=1, maxLength=32),
                "runtime": _obj(
                    {
                        "engine": _string(r"^[a-z][a-z0-9_-]{1,31}$"),
                        "engine_version": _string(minLength=1, maxLength=64),
                        "backend": {"enum": ["onnxruntime", "pytorch", "tensorrt",
                                             "python"]},
                        "tensorrt_version": _string(_EXACT_VERSION),
                        "cuda": _string(minLength=1, maxLength=32),
                        "driver_min": _string(minLength=1, maxLength=32),
                        "gpu_architectures": {
                            "type": "array", "minItems": 1, "uniqueItems": True,
                            "items": _string(_GPU_ARCH),
                        },
                        "gpu_memory_mib": {"type": "integer", "minimum": 0},
                    },
                    ["engine", "engine_version", "backend"],
                ),
            },
            ["medicalos_api", "service_contract"],
        ),
    },
}


# =====================================================================================
# ModelVersion -- chapter 6 section 6.5
# =====================================================================================
_IO_TENSOR_PROPS: dict[str, Any] = {
    "name": _string(minLength=1, maxLength=64),
    "shape": {"type": "array", "minItems": 2, "maxItems": 6,
              "items": {"type": "integer", "minimum": 1}},
    "dtype": {"enum": ["float32", "float16", "int8", "uint8", "int16", "int32"]},
    "layout": _string(r"^[NCZYXDHW]{2,6}$"),
    "value_range": {"type": "array", "minItems": 2, "maxItems": 2,
                    "items": {"type": "number"}},
}

MODEL_VERSION_SPEC: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"{BASE_URI}model_version/{SCHEMA_VERSION}.json",
    "title": "MedicalOS ModelVersion manifest spec block",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "capabilities", "weights_availability", "preprocessing_spec_ref", "io",
        "runtime", "evaluation_run_id", "not_validated_for", "known_failure_modes",
    ],
    "properties": {
        "capabilities": {
            "type": "array", "minItems": 1, "uniqueItems": True,
            "items": _string(_CAPABILITY),
        },
        "weights_availability": {"enum": ["platform_managed", "vendor_sealed"]},
        # `MOS-REG-039`: null for `vendor_sealed`, required for `platform_managed`. The
        # `allOf` at the bottom of this schema is where that is enforced; the shape here
        # admits null so the sealed case can state it explicitly rather than by omission.
        "weights": _obj(
            {
                "oci_ref": _string(_OCI_REF),
                "digest": _string(_SHA256),
                "format": {"enum": ["onnx", "torchscript", "tensorrt_plan", "safetensors"]},
                "size_bytes": {"type": "integer", "minimum": 1},
            },
            ["oci_ref", "digest", "format", "size_bytes"],
            nullable=True,
        ),
        "preprocessing_spec_ref": _string(_ARTIFACT_REF),
        # `MOS-IMG-049` and known-inconsistency 2: the `sha256:` prefix, NOT bare hex.
        # See this module's docstring -- `MOS-REG-036` says the opposite and chapter 4
        # owns the field.
        "golden_fixture": _obj(
            {
                "sha256": _string(_SHA256),
                "output_tensor_sha256": _string(_SHA256),
                "output_shape": {"type": "array", "minItems": 2, "maxItems": 6,
                                 "items": {"type": "integer", "minimum": 1}},
            },
            ["sha256", "output_tensor_sha256"],
            nullable=True,
        ),
        "io": _obj(
            {
                "input": _obj(
                    {
                        **_IO_TENSOR_PROPS,
                        # `MOS-REG-037`: stated explicitly, never defaulted. "A model
                        # served with the wrong orientation returns a plausible mirrored
                        # segmentation that passes every structural check."
                        "orientation": _string(r"^[LRAPSI]{3}$"),
                    },
                    ["name", "shape", "dtype", "layout", "orientation"],
                ),
                "output": _obj(
                    {
                        **_IO_TENSOR_PROPS,
                        # `MOS-REG-033`: binary and fractional segmentation are
                        # different outputs and the manifest MUST discriminate them.
                        "kind": {
                            "enum": ["segmentation_logits", "segmentation_binary",
                                     "segmentation_fractional", "detection_boxes",
                                     "scalar"]
                        },
                        "label_map": {
                            "type": "object",
                            "propertyNames": {"type": "string", "pattern": r"^[0-9]{1,4}$"},
                            "additionalProperties": _string(r"^[a-z][a-z0-9_]{0,63}$"),
                        },
                    },
                    ["name", "shape", "dtype", "layout", "kind"],
                ),
            },
            ["input", "output"],
        ),
        "operating_point": _obj(
            {
                "kind": {"enum": ["probability_threshold", "logit_threshold",
                                  "argmax", "none"]},
                # `MOS-REG-113`: one name for this number, platform-wide.
                "score_threshold": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "selected_on_evaluation_run": _string(r"^er_[0-9A-HJKMNP-TV-Z]{10,26}$"),
                "selection_rule": _string(minLength=1, maxLength=256),
            },
            ["kind", "selected_on_evaluation_run", "selection_rule"],
            nullable=True,
        ),
        "runtime": _obj(
            {
                # `MOS-REG-101`: addressed through the `engine` discriminator, never
                # assumed. No field is named after a specific inference server.
                "engine": _string(r"^[a-z][a-z0-9_-]{1,31}$"),
                "engine_version": _string(minLength=1, maxLength=64),
                "backend": {"enum": ["onnxruntime", "pytorch", "tensorrt", "python"]},
                "gpu_architectures": {
                    "type": "array", "minItems": 1, "uniqueItems": True,
                    "items": _string(_GPU_ARCH),
                },
                "cuda": _string(minLength=1, maxLength=32),
                "tensorrt_version": _string(minLength=1, maxLength=32),
                "driver_min": _string(minLength=1, maxLength=32),
                "gpu_memory_mib": {"type": "integer", "minimum": 0},
            },
            ["engine", "engine_version", "backend"],
        ),
        # §6.10.2: a converted model is a NEW version with `derived_from` set, its own
        # evaluation run (`MOS-REG-103`) and a recorded numeric equivalence check
        # (`MOS-REG-104`).
        "derived_from": {"type": ["string", "null"], "pattern": _ARTIFACT_REF},
        "conversion_equivalence": _obj(
            {
                "fixture_digest": _string(_SHA256),
                "max_abs_diff": {"type": "number", "minimum": 0.0},
                "post_threshold_dice": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            ["fixture_digest", "max_abs_diff", "post_threshold_dice"],
            nullable=True,
        ),
        # `MOS-REG-031`: a foreign key into `EvaluationRun`, and NO free-form metrics map
        # on a ModelVersion. Metrics are obtained by dereferencing the run.
        "evaluation_run_id": _string(r"^er_[0-9A-HJKMNP-TV-Z]{10,26}$"),
        "applicability_envelope_ref": _string(r"^ae_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$"),
        # `MOS-REG-032`: at least one entry each. "None known" is expressible only as the
        # literal sentence, which is a claim the publisher makes and which appears
        # verbatim in the provenance panel.
        "not_validated_for": {
            "type": "array", "minItems": 1,
            "items": _string(minLength=1, maxLength=512),
        },
        "known_failure_modes": {
            "type": "array", "minItems": 1,
            "items": _string(minLength=1, maxLength=512),
        },
    },
    "allOf": [
        # `MOS-REG-039`, first half: platform_managed MUST declare weights and a golden
        # fixture (`MOS-REG-036` makes the fixture the input to the worker self-test).
        {
            "if": {"properties": {"weights_availability": {"const": "platform_managed"}},
                   "required": ["weights_availability"]},
            "then": {"required": ["weights", "golden_fixture"],
                     "properties": {"weights": {"type": "object"},
                                    "golden_fixture": {"type": "object"}}},
        },
        # `MOS-REG-039`, second half: a sealed model declares neither. Its ModelVersion
        # exists so evidence and provenance have a stable subject (`MOS-REG-026`,
        # `MOS-REG-030`), not so it can claim weights nobody can verify.
        {
            "if": {"properties": {"weights_availability": {"const": "vendor_sealed"}},
                   "required": ["weights_availability"]},
            "then": {"properties": {"weights": {"type": "null"},
                                    "golden_fixture": {"type": "null"}}},
        },
        # `MOS-REG-033`: logits and fractional output MUST carry an operating point.
        # Without one, no reported sensitivity or F1 has a threshold attached
        # (`MOS-REG-034`).
        {
            "if": {
                "properties": {
                    "io": {"properties": {"output": {"properties": {"kind": {
                        "enum": ["segmentation_logits", "segmentation_fractional"]}}}}}
                },
                "required": ["io"],
            },
            "then": {"required": ["operating_point"],
                     "properties": {"operating_point": {"type": "object",
                                                        "required": ["score_threshold"]}}},
        },
        # `MOS-REG-098`: a serialized TensorRT plan is compiled for one TensorRT version
        # and one set of architectures. A floor expresses neither, so the plan case
        # requires exact pins and a non-empty architecture list.
        {
            "if": {
                "properties": {"weights": {"type": "object",
                                           "properties": {"format": {"const": "tensorrt_plan"}},
                                           "required": ["format"]}},
                "required": ["weights"],
            },
            "then": {
                "properties": {
                    "runtime": {
                        "type": "object",
                        "required": ["tensorrt_version", "cuda", "gpu_architectures",
                                     "driver_min"],
                        "properties": {
                            "tensorrt_version": _string(_EXACT_VERSION),
                            "cuda": _string(_EXACT_VERSION),
                            "gpu_architectures": {"type": "array", "minItems": 1},
                        },
                    }
                }
            },
        },
    ],
}


# =====================================================================================
# The common envelope -- chapter 6 section 6.3, verbatim plus the closure MOS-REG-015
# requires and one `allOf` branch per seeded kind.
# =====================================================================================
ENVELOPE_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"{BASE_URI}envelope/{SCHEMA_VERSION}.json",
    "title": "MedicalOS artifact envelope",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "kind", "family", "version", "content_digest",
                 "publisher", "created_at", "spec"],
    "properties": {
        "schema_version": _string(r"^1\.[0-9]+\.[0-9]+$"),
        "kind": {
            # `MOS-REG-013`: the discriminator, and the enum is CLOSED. A manifest whose
            # kind is not here is 422, never a row.
            "enum": ["service_version", "model_version", "preprocessing_spec",
                     "dataset_version", "annotation_set", "workflow_version",
                     "policy_set_version"]
        },
        "family": _string(
            r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$",
            maxLength=128,
        ),
        "version": _string(_SEMVER),
        # `MOS-REG-017`: present, and compared against the value the registry computes.
        # The registry never TAKES it: `repo.publish` recomputes and refuses a mismatch.
        "content_digest": _string(_SHA256),
        "publisher": _obj(
            {
                "org_id": _string(r"^org_[0-9A-Za-z][0-9A-Za-z_.-]{1,63}$"),
                # `MOS-REG-028`: verified against the identities registered for
                # `legal_manufacturer.id`. See `repo._verify_supply_chain` for what that
                # check can and cannot do in this release.
                "signing_identity": _string(minLength=1, maxLength=512),
            },
            ["org_id", "signing_identity"],
        ),
        "created_at": _string(format="date-time"),
        "spec": {"type": "object"},
    },
    "allOf": [
        {
            "if": {"properties": {"kind": {"const": "model_version"}},
                   "required": ["kind"]},
            "then": {"properties": {"spec": {"$ref": MODEL_VERSION_SPEC["$id"]}}},
        },
        {
            "if": {"properties": {"kind": {"const": "service_version"}},
                   "required": ["kind"]},
            "then": {"properties": {"spec": {"$ref": SERVICE_VERSION_SPEC["$id"]}}},
        },
    ],
}


SCHEMAS: Final[dict[str, dict[str, Any]]] = {
    ENVELOPE_SCHEMA["$id"]: ENVELOPE_SCHEMA,
    SERVICE_VERSION_SPEC["$id"]: SERVICE_VERSION_SPEC,
    MODEL_VERSION_SPEC["$id"]: MODEL_VERSION_SPEC,
}

# The build-time gate. Every schema in the source is checked for an unknown keyword, an
# unresolvable `$ref` and a missing `additionalProperties: false` AT IMPORT, so a mistyped
# constraint is an ImportError in CI rather than a manifest accepted in production.
for _id, _schema in SCHEMAS.items():
    check_schema(_schema, registry=SCHEMAS)
    assert_closed_objects(_schema, path=_id)


# =====================================================================================
# Lookup, validation, and the generated seed
# =====================================================================================
def schema_id_for_kind(kind: str, schema_version: str = SCHEMA_VERSION) -> str:
    """`$id` for either vocabulary's spelling of the kind. `MOS-STORE-253`."""
    version_kind = VERSION_KIND.get(kind, kind)
    if version_kind not in FAMILY_KIND:
        raise SchemaError(f"{kind!r} is not an artifact kind (MOS-REG-013)")
    return f"{BASE_URI}{version_kind}/{schema_version}.json"


def schema_for_kind(kind: str, schema_version: str = SCHEMA_VERSION) -> dict[str, Any]:
    """The per-kind SPEC schema. Raises `SchemaError` for a kind with no schema yet."""
    ident = schema_id_for_kind(kind, schema_version)
    schema = SCHEMAS.get(ident)
    if schema is None:
        raise SchemaError(
            f"no manifest schema is registered for kind {kind!r} at {schema_version}; "
            "see medos/medos/registry/schemas.py for which kinds this release seeds and why"
        )
    return schema


def manifest_errors(manifest: Any) -> list[str]:
    """Every violation of the envelope (and, through it, the per-kind spec schema).

    `MOS-REG-014`: validation happens BEFORE the row is written and a failure is a hard
    reject. This function is the whole of that check; `repo.publish` calls it first and
    writes nothing when it returns a non-empty list.
    """
    return [str(v) for v in iter_errors(manifest, ENVELOPE_SCHEMA, registry=SCHEMAS)]


def schema_rows() -> list[dict[str, Any]]:
    """The `artifact_manifest_schemas` rows, generated. One per seeded FAMILY-level kind.

    `schema_digest` is `sha256:` + the digest of the RFC 8785 canonicalisation of the
    schema document, the same convention `artifacts.manifest_digest` uses, so "is the
    database carrying the schema this source states?" is one string comparison.
    """
    rows: list[dict[str, Any]] = []
    for family_kind in SEEDED_KINDS:
        schema = schema_for_kind(family_kind)
        rows.append(
            {
                "kind": family_kind,
                "schema_version": SCHEMA_VERSION,
                "json_schema": schema,
                "schema_digest": "sha256:" + sha256_hex(canonical_bytes(schema)),
                "introduced_in": INTRODUCED_IN,
            }
        )
    return rows


def sql_seed() -> str:
    """The INSERT block `0012_artifacts.up.sql` carries, GENERATED. `MOS-REG-016`.

    Regenerate with `python -m medos.registry.schemas` and paste between the two marker
    comments in the migration. The migration file is immutable once applied
    (`medos/medos/db/migrate.py` refuses a changed digest), so a schema change after 0012 has
    run anywhere is a NEW migration inserting a new `(kind, schema_version)` row --
    which is exactly what `MOS-REG-015`'s "versioned independently per kind" means.
    `tests/integration/test_artifacts.py` re-derives these rows and compares them to the
    applied database, so a hand-edit of the SQL below fails the suite.
    """
    lines = [
        "INSERT INTO artifact_manifest_schemas "
        "(kind, schema_version, json_schema, schema_digest, introduced_in) VALUES"
    ]
    parts = []
    for row in schema_rows():
        literal = json.dumps(row["json_schema"], sort_keys=True, separators=(",", ":"))
        parts.append(
            f"  ('{row['kind']}', '{row['schema_version']}',\n"
            f"   $json${literal}$json$::jsonb,\n"
            f"   '{row['schema_digest']}', '{row['introduced_in']}')"
        )
    lines.append(",\n".join(parts) + ";")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - a generator, run by hand
    print(sql_seed())
