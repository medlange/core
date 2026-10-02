# SPDX-License-Identifier: Apache-2.0
"""The platform copied the patient's name and stamped "identity removed" over it.

WHAT THE TWO HALVES DO
----------------------
`medos.writer.identity.apply_inherited_attributes` copies `PatientName` and `PatientID`
verbatim from the source instance -- `MOS-IMG-090` marks them "copy", and substituting a
value is a defect. `apply_ai_marking` then writes

    obj.PatientIdentityRemoved = "NO" if identity.uid_space == "source" else "YES"

which is `MOS-IMG-089`. Each is correct on its own. Together, on an identified source with
`uid_space = "deid"`, they produce an object that CLAIMS to be de-identified while
carrying the patient's name -- worse than either honest alternative, because a consumer
that trusts the flag has been told it may stop being careful.

WHERE THE GUARD WAS
-------------------
In `spikes/week0/write_dicom_results.py`, and only there. CONTRACT.md section 2 says the
spike's logic is lifted into the platform by MOVING, not rewriting; this function was the
one that did not come. The platform kept both halves and not the check between them, for
five months, while a test suite of this size reported green.

It was latent, not live: nothing in this repository builds a `JobIdentity` with
`uid_space = "deid"`. That is exactly why it survived -- a guard for a path nobody takes
reads as dead code, and the day somebody takes the path is the day it is needed.

WHAT THIS TEST IS
-----------------
Four cases and a negative control. The control is the one that matters: it asserts that
with the guard removed the two halves really do produce the bad object, so this file is
not testing that an `if` statement returns early.
"""

from __future__ import annotations

from typing import Any

import pytest
from medos.core.errors import SystemFailure
from medos.writer.identity import apply_inherited_attributes, guard_deid_uid_space
from pydicom.dataset import Dataset


class _Identity:
    """The two fields the guard reads, and the one its message quotes."""

    def __init__(self, uid_space: str) -> None:
        self.uid_space = uid_space
        self.study_instance_uid = "1.2.826.0.1.3680043.8.498.1"


def _identified_source() -> Dataset:
    """A source instance as a hospital PACS holds one: named, and not tagged."""
    src = Dataset()
    src.PatientName = "DOE^JANE"
    src.PatientID = "MRN-4417"
    src.StudyInstanceUID = "1.2.826.0.1.3680043.8.498.1"
    src.StudyDate = "20260101"
    src.StudyTime = "101500"
    return src


def _deidentified_source() -> Dataset:
    src = _identified_source()
    src.PatientName = "ANON^ANON"
    src.PatientID = "TCGA-00-0000"
    src.PatientIdentityRemoved = "YES"
    return src


def test_the_source_uid_space_is_always_permitted() -> None:
    """`MOS-DATA-020`: the platform writer's default, and the only path in use today."""
    guard_deid_uid_space(_identified_source(), _Identity("source"))


def test_a_deid_write_over_a_tagged_source_is_permitted() -> None:
    """`MOS-DATA-036` translated the UIDs and tagged the instances before the writer."""
    guard_deid_uid_space(_deidentified_source(), _Identity("deid"))


def test_a_deid_write_over_an_identified_source_is_refused() -> None:
    with pytest.raises(SystemFailure) as exc:
        guard_deid_uid_space(_identified_source(), _Identity("deid"))

    problem = exc.value
    assert problem.reason_code == "deid_uid_space_over_identified_source", (
        problem.reason_code
    )
    assert problem.detail["uid_space"] == "deid"
    assert problem.detail["source_patient_identity_removed"] == ""
    message = problem.message
    assert "MOS-IMG-090" in message and "MOS-IMG-089" in message, (
        "the refusal must name both halves. 'the writer refused' sends a reader to the "
        "call site; naming the copy rule and the flag rule sends them to the conflict"
    )
    assert "MOS-DATA-020" in message, "the safe alternative is not offered"


def test_no_flag_can_talk_the_guard_out_of_it() -> None:
    """The spike had `--source-is-deidentified`; the platform deliberately does not.

    A writer argument that overrides a safety check is the per-job override
    `MOS-TRAIN-072` refuses by name elsewhere in this platform. The corpus-is-untagged
    case belongs in a recorded provenance declaration (`medos/deploy/provenance/`), not in
    an argument somebody passes once.
    """
    import inspect

    parameters = set(inspect.signature(guard_deid_uid_space).parameters)
    assert parameters == {"src", "identity"}, (
        f"guard_deid_uid_space takes {sorted(parameters)}. A third parameter is almost "
        "certainly an override, and an override here re-opens the hazard this function "
        "closed"
    )


def test_without_the_guard_the_two_halves_really_do_produce_the_bad_object() -> None:
    """THE NEGATIVE CONTROL. Without it the four checks above prove only that an `if` works.

    This runs the two real functions -- `MOS-IMG-090`'s copy and `MOS-IMG-089`'s flag --
    in the order the writers run them, with the guard not called, and asserts the object
    that comes out carries the patient's name under `PatientIdentityRemoved = YES`. That
    object is what `medos/medos/writer/` produced on this path until the lift.
    """
    src = _identified_source()
    obj = Dataset()
    apply_inherited_attributes(obj, src, is_seg=True)

    # The one line of `apply_ai_marking` this is about, run without the guard above it.
    identity: Any = _Identity("deid")
    obj.PatientIdentityRemoved = "NO" if identity.uid_space == "source" else "YES"

    assert obj.PatientIdentityRemoved == "YES"
    assert str(obj.PatientName) == "DOE^JANE", (
        "if this stops being a real name the control has stopped controlling: the whole "
        "point is that MOS-IMG-090 copies the identifier verbatim"
    )
    assert obj.PatientID == "MRN-4417"

    # And the guard, given the same inputs, refuses to let that object exist.
    with pytest.raises(SystemFailure):
        guard_deid_uid_space(src, identity)


def test_apply_ai_marking_itself_refuses_not_just_the_guard_function() -> None:
    """The guard has to be CALLED, and by the function that writes the flag.

    Every check above exercises `guard_deid_uid_space` directly, which proves the function
    is right and nothing about whether anybody runs it. Deleting the one call from
    `apply_ai_marking` would leave all of them green -- so this one goes through the
    writer's own entry point, which is the site the hazard lives at.
    """
    from medos.writer.identity import apply_ai_marking

    src = _identified_source()
    obj = Dataset()
    apply_inherited_attributes(obj, src, is_seg=True)

    with pytest.raises(SystemFailure) as exc:
        apply_ai_marking(obj, _full_identity("deid"), _Plan(), is_seg=True, src=src)
    assert exc.value.reason_code == "deid_uid_space_over_identified_source"

    # ... and the same call in the source UID space writes the object it always did.
    # `is_seg=False`: the SEG branch pins a DimensionOrganizationUID off SOPInstanceUID,
    # which is a different rule and not what this test is about.
    apply_ai_marking(obj, _full_identity("source"), _Plan(), is_seg=False, src=src)
    assert obj.PatientIdentityRemoved == "NO"
    assert obj.ContributingEquipmentSequence[0].Manufacturer == "MedicalOS"


class _Plan:
    """The one member `apply_ai_marking` reads off an `OutputPlan`."""

    provenance_record_uid = "1.2.826.0.1.3680043.8.498.99"


def _full_identity(uid_space: str) -> Any:
    """`_Identity` plus the fields `apply_ai_marking` -- not the guard -- reads."""
    identity = _Identity(uid_space)
    identity.medicalos_version = "0.4.0"
    identity.deployment_id = "dep_medos_slice"
    identity.service_id = "lung_nodule"
    identity.service_version = "1.0.0"
    identity.clinical_use_mode = "RESEARCH_ONLY"
    identity.job_id = "job_01J0000000000000000000000"
    identity.preprocessing_spec_digest = "sha256:" + "0" * 64
    return identity
