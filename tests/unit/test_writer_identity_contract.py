# SPDX-License-Identifier: Apache-2.0
"""Two writer rules that had no test outside `spikes/week0/test_contracts.py`.

Measured across `tests/` before the spike was deleted, by grepping for each function the
spike's `run_identity_tests` exercised:

    derive_uid                    4 files
    derive_idempotency_key        4 files
    plan_outputs                  3 files
    measure_volume_ml             2 files
    ai_series_description         1 file
    normalise_pn                  0
    validate_equipment_identity   0

Most of that body was genuinely covered elsewhere and did not need moving. These two were
not, and they are not small: `MOS-IMG-076`/`077`/`078` are the rule that the platform must
REFUSE rather than write a placeholder manufacturer into a clinical object, and
`MOS-IMG-092`/`135` are what keep the `[AI]` marking on a SeriesDescription that a
64-character VR is about to truncate.

The bodies are the spike's, turned into test functions. `_identity()` is its helper.
"""

from __future__ import annotations

from typing import Any

import pytest
from medos.core.uids import JobIdentity
from medos.writer.identity import ai_series_description, normalise_pn

STUDY_UID = "1.2.826.0.1.3680043.10.777.1"
SERIES_UID = "1.2.826.0.1.3680043.10.777.2"


def _identity(**overrides: Any) -> JobIdentity:
    base: dict[str, Any] = {
        "tenant_id": "t", "service_id": "svc.demo", "service_version": "1.0.0",
        "model_id": "m", "model_version": "1.0.0", "deployment_id": "dep_test",
        "institution_name": "Test Hospital",
        "legal_manufacturer_name": "MedicalOS B.V.", "medicalos_version": "0.2.0",
        "preprocessing_spec_version": "0.1.0",
        "preprocessing_spec_digest": "sha256:" + "0" * 64,
        "study_instance_uid": STUDY_UID, "selected_series_uids": (SERIES_UID,),
        "requested_outputs": ("SEG", "SR"), "parameters": {}, "uid_space": "source",
        "org_root": None, "series_number_band": 9000,
        "clinical_use_mode": "RESEARCH_ONLY", "job_id": "job_test",
    }
    base.update(overrides)
    return JobIdentity(**base)  # type: ignore[arg-type]


def test_a_valid_equipment_identity_is_accepted() -> None:
    """The control. Without it the four refusals below pass on a validator that says no
    to everything."""
    _identity().validate_equipment_identity()


@pytest.mark.parametrize(
    "override,why",
    [
        ({"legal_manufacturer_name": "  "}, "empty Manufacturer"),
        ({"legal_manufacturer_name": "A" * 65}, "over 64 chars"),
        ({"legal_manufacturer_name": "Acme\\Corp"}, "backslash"),
        ({"deployment_id": "10.0.1.42"}, "IP as StationName"),
    ],
)
def test_an_invalid_equipment_identity_is_refused(override: dict, why: str) -> None:
    """MOS-IMG-076/077/078: refuse BEFORE building, never write a placeholder.

    The failure mode this forbids is not a crash. It is a DICOM object that reaches a
    PACS carrying `Manufacturer` = a blank, a truncated legal name, or a host address
    where a station name belongs -- an object nobody can trace back to what produced it,
    which is the whole point of `MOS-IMG-076`.
    """
    with pytest.raises(ValueError):
        _identity(**override).validate_equipment_identity()


def test_the_ai_marking_survives_truncation() -> None:
    """MOS-IMG-133/135/136: SeriesDescription is VR LO, and the prefix must not be what
    falls off.

    A 200-character description truncated to 64 from the left loses `[AI][RUO] ` first --
    which turns a research-only object into one that reads as an ordinary clinical series
    on every viewer that shows the description.
    """
    long = "x" * 200
    clinical = ai_series_description(long, "CLINICAL")
    ruo = ai_series_description(long, "RESEARCH_ONLY")
    assert clinical.startswith("[AI] ") and len(clinical) == 64, len(clinical)
    assert ruo.startswith("[AI][RUO] ") and len(ruo) == 64, len(ruo)


def test_content_creator_name_is_normalised_to_one_pn_component() -> None:
    """MOS-IMG-092: ContentCreatorName is VR PN, one component group, 64 characters.

    `^`, `=` and `\\` are PN's own delimiters. Left in a value, they do not make an
    invalid file -- they make a name that a reader parses into component groups nobody
    intended, which is worse than a rejected write.
    """
    assert normalise_pn("Acme^Corp=X\\Y") == "AcmeCorpXY"
    assert len(normalise_pn("N" * 100)) == 64
