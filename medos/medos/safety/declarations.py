# SPDX-License-Identifier: Apache-2.0
"""The `ApplicabilityEnvelope` this deployment's one ServiceVersion declares.

`MOS-EVID-095`: "Every ServiceVersion that declares a capability MUST carry an
`ApplicabilityEnvelope` version. A ServiceVersion without one MUST NOT be deployable in
`clinical` mode." This deployment has one ServiceVersion -- `medos.slice/0.1.0`, the
stand-in `medos.api.routes_jobs.SLICE_SERVICE_VERSION_ID` names -- and this file is its
envelope.

WHY THE NUMBERS ARE WHAT THEY ARE, AND WHAT THEY ARE NOT
---------------------------------------------------------
`derivation: declared`, and `derived_from_evaluation_run: null`. That is the honest value
and it is the whole point of the field.

`MOS-EVID-097` requires numeric bounds to be DERIVABLE from the `acquisition_profile` of
the DatasetVersion behind an `EvaluationRun` -- `percentile_1_99` or `min_max`. There is
no `EvaluationRun` in this deployment: `evaluation_runs` is the measuring half of 0.2.0
and lands on top of `0006_evidence`. So no bound here is derived from a cohort, and
declaring `derivation: percentile_1_99` with a null run would be a claim that a
measurement was made.

What the bounds ARE is the envelope of what the METHOD can be reasoned about -- the same
posture `medos/medos/capabilities/lung_segmentation.py` already takes in its own comment: "There
is no validation cohort behind these numbers, so they are the envelope of what the METHOD
can be reasoned about, not of what it was measured on." They are copied from the
constraints those capabilities already enforce in code, so that declaring the envelope
does not silently CHANGE which studies this deployment accepts; what changes is that the
constraint is now data with a content address, evaluated by the platform before the
service runs (`MOS-EVID-100`), and widened only by publishing a new version.

`MOS-EVID-098`'s narrow-only rule therefore binds from here forward: version 2 of this
envelope may narrow any bound freely, and may not widen one without an `EvaluationRun` on
a DatasetVersion whose `acquisition_profile` covers the widened range.

`MOS-EVID-099`: "`not_validated_for` entries on the ServiceVersion (Chapter 9) MUST be
consistent with the envelope: every population or acquisition condition listed there MUST
be `OUT` or absent from `values`." The capability metadata in `medos/medos/capabilities/` lists
"paediatric patients under 18 years" and thick-slice reconstructions among its
`not_validated_for` entries; `patient_age_years: {min: 18}` and
`slice_thickness_mm: {max: 3.0}` are the envelope rows that make those two `OUT`, and
`consistency_violations()` below is the check `MOS-EVID-099` requires CI to run.

Spec: MOS-EVID-095, MOS-EVID-097, MOS-EVID-098, MOS-EVID-099, MOS-EVID-101, MOS-SAFE-015.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from medos.safety.envelope import ApplicabilityEnvelope, evaluate

__all__ = [
    "SLICE_SERVICE_ID",
    "SLICE_SERVICE_VERSION",
    "SLICE_ENVELOPE_MANIFEST",
    "slice_envelope",
    "consistency_violations",
]

# `medos.api.routes_jobs.SLICE_SERVICE_VERSION_ID` is `"medos.slice/0.1.0"`; the `jobs`
# row splits it into `service_id` and `service_version`, and the envelope is keyed on the
# split form because that is what a worker has in hand.
SLICE_SERVICE_ID = "medos.slice"
SLICE_SERVICE_VERSION = "0.1.0"

SLICE_ENVELOPE_MANIFEST: Mapping[str, Any] = {
    "apiVersion": "medicalos.io/v1",
    "kind": "ApplicabilityEnvelope",
    "metadata": {
        "subject_kind": "service_version",
        "subject_id": SLICE_SERVICE_ID,
        "subject_version": SLICE_SERVICE_VERSION,
        "version": 1,
        "derivation": "declared",
        "derived_from_evaluation_run": None,
    },
    "spec": {
        "constraints": [
            # The thin-recon requirement the capabilities already enforce. `marginal_max`
            # at 5.0 rather than absent: a 4 mm archival recon is a study a site will
            # legitimately want flagged rather than refused, and `MOS-EVID-101`'s `flag`
            # policy renders it to the reader instead of dropping it.
            {
                "attribute": "slice_thickness_mm",
                "type": "range",
                "min": 0.5,
                "max": 3.0,
                "marginal_max": 5.0,
            },
            # In-plane resolution. No marginal band on the upper side: beyond 1.5 mm the
            # HU-threshold methods in `medos/medos/capabilities/` are measuring partial-volume
            # averaging rather than tissue, and a flagged result there would be a number
            # with a warning next to it rather than a number.
            {"attribute": "pixel_spacing_mm_max", "type": "range", "max": 1.5},
            # Thoracic coverage. `marginal_min` 120 mm: a study that stops short of the
            # apices produces a lung volume that is an underestimate of unknown size,
            # which is precisely what a flag is for.
            {
                "attribute": "z_coverage_mm",
                "type": "range",
                "min": 150.0,
                "marginal_min": 120.0,
            },
            {"attribute": "instance_count", "type": "range", "min": 40},
            # MOS-EVID-099: `not_validated_for` names paediatric patients.
            {"attribute": "patient_age_years", "type": "range", "min": 18, "max": 120},
            # MOS-DATA-060.2's classes. SHARP is marginal and not OUT because a sharp
            # kernel raises %LAA rather than making the segmentation wrong, and
            # `medos/medos/capabilities/emphysema_laa.py` already says so in its own failure
            # modes -- a flagged number with the kernel named is more useful to a reader
            # than a refusal.
            {
                "attribute": "convolution_kernel_class",
                "type": "enum_in",
                "values": ["SOFT", "STANDARD"],
                "marginal_values": ["SHARP"],
            },
            # Any scanner vendor, but the study lands MARGINAL and is annotated, because
            # no vendor here has a cohort behind it. `MOS-EVID-095`'s
            # `marginal_values_allowed` is exactly this claim.
            {
                "attribute": "manufacturer",
                "type": "enum_in",
                "values": ["SIEMENS", "GE MEDICAL SYSTEMS", "Philips", "CANON MEDICAL SYSTEMS"],
                "marginal_values_allowed": True,
            },
            {"attribute": "body_part_examined", "type": "enum_in", "values": ["CHEST"]},
            # A contrast-enhanced chest CT shifts lung-parenchyma HU only marginally, but
            # it shifts the mediastinal and vascular structures a threshold method uses as
            # its boundary a great deal. NONE is validated; the enhanced phases are
            # tolerated and flagged; `UNKNOWN` is neither, and lands OUT -- which is
            # `MOS-DATA-060.5`'s own rule ("a selector that requires a phase MUST reject
            # it") applied at the envelope.
            {
                "attribute": "contrast_phase",
                "type": "enum_in",
                "values": ["NONE"],
                "marginal_values": ["ARTERIAL", "PORTAL_VENOUS", "DELAYED",
                                    "PULMONARY_ARTERIAL"],
            },
        ],
        "marginal_policy_default": "flag",
    },
}


def slice_envelope() -> ApplicabilityEnvelope:
    """The declaration, parsed. Built each call; it is frozen and cheap.

    Not a module-level constant, because `from_manifest` RAISES on a malformed
    declaration and a module-level constant would turn that into an ImportError three
    imports away from the cause.
    """
    return ApplicabilityEnvelope.from_manifest(SLICE_ENVELOPE_MANIFEST)


def consistency_violations(not_validated_for: Mapping[str, Any]) -> list[str]:
    """`MOS-EVID-099`: every `not_validated_for` condition MUST be `OUT` or undeclared.

    "CI MUST assert this and fail the ServiceVersion publish on inconsistency."

    `not_validated_for` on a `CapabilityMetadata` is prose (`MOS-SAFE-015` requires at
    least one entry and says nothing about its form), so this function cannot parse the
    sentences. What it CAN do -- and what the requirement is actually protecting against --
    is take the machine-readable conditions a caller extracts from those sentences and
    assert that the envelope refuses each one. The caller passes
    `{"paediatric": {"patient_age_years": 8}, ...}`; this returns the names of any that
    the envelope does NOT refuse.

    Stated plainly because it is a narrowing: this checks the conditions a human mapped out
    of the prose, not the prose. The alternative -- regex over clinical English -- would
    pass for the wrong reason and is worse than an honest partial check.
    """
    envelope = slice_envelope()
    failures: list[str] = []
    for name, attributes in not_validated_for.items():
        verdict = evaluate(envelope, dict(attributes), marginal_policy="flag")
        if verdict.zone != "OUT":
            failures.append(
                f"{name}: MOS-EVID-099 requires a not_validated_for condition to be OUT, "
                f"but the envelope evaluates {dict(attributes)} to {verdict.zone}"
            )
    return failures
