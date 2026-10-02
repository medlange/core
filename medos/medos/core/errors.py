# SPDX-License-Identifier: Apache-2.0
"""MedicalOS error hierarchy and its RFC 9457 problem-class mapping.

CONTRACT.md §9: every error surfaced by the HTTP layer is an
`application/problem+json` document whose `class` field separates a *clinical*
rejection from a *transport* failure and from a *system* failure. Chapter 5
(`MOS-EXEC-001`) makes the same split a job-state distinction: a clinical rejection is
the terminal state `REJECTED`, everything else is `FAILED`.

WHY the split is in the type system rather than in a status-code table: `REJECTED` is a
result, not an incident. A pager that fires on "no eligible series" trains its operators
to ignore it, and a UI that renders a clinical rejection as a crash makes clinicians
distrust every subsequent green run. The exception class is therefore what decides the
state, the problem `class`, and whether a retry is even meaningful.

This module is the bottom of the `medos.core` import graph: it imports nothing from
`medos` so that `geometry` and `dicomio` can both raise without a cycle.

Lifted from `spikes/week0/build_volume.py` (`REJECTION_CODES`, `SeriesSelectionError`,
`GeometryRejection`) per CONTRACT.md §2. The only change to the lifted bodies is the base
class: they now sit under `ClinicalRejection` instead of bare `Exception`.

Spec: MOS-IMG-009, MOS-IMG-010, MOS-IMG-011, MOS-EXEC-001.
"""

from __future__ import annotations

from typing import Any, Literal

__all__ = [
    "ProblemClass",
    "MedosError",
    "ClinicalRejection",
    "TransportFailure",
    "SystemFailure",
    "SeriesSelectionError",
    "GeometryRejection",
    "REJECTION_CODES",
]

# CONTRACT.md §9. The enum is closed; adding a member is an API minor version.
ProblemClass = Literal["clinical_rejection", "transport_failure", "system_failure"]


class MedosError(Exception):
    """Base of every error MedicalOS raises deliberately.

    Carries the three fields the RFC 9457 renderer needs and nothing else: `reason_code`
    is the machine-readable discriminator, `detail` is the structured evidence, `message`
    is prose for a human. `problem_class` is a class attribute, not an instance field,
    because the class *is* the classification -- deciding it per-instance is how a
    clinical rejection ends up paging someone at 03:00.

    `retryable` exists so `JobQueue.fail(..., retryable=)` (CONTRACT.md §4) never has to
    guess. A clinical rejection is terminal by definition: the same inputs will be
    rejected again.

    MUST NOT carry PHI. `detail` holds UIDs, counts and millimetres -- never a name, an
    MRN or a date of birth (CONTRACT.md §11).
    """

    problem_class: ProblemClass = "system_failure"
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        reason_code: str,
        detail: dict[str, Any] | None = None,
        message: str | None = None,
    ) -> None:
        super().__init__(message or reason_code)
        self.reason_code = reason_code
        self.detail: dict[str, Any] = detail or {}
        self.message = message or reason_code

    def to_problem(self, *, instance: str | None = None) -> dict[str, Any]:
        """Render as an RFC 9457 problem document (CONTRACT.md §9).

        `type` is a stable URN built from the reason code rather than a dereferenceable
        URL: the codes outlive any docs hostname, and RFC 9457 §4.2 permits a URI that is
        not retrievable.
        """
        problem: dict[str, Any] = {
            "type": f"urn:medos:problem:{self.reason_code}",
            "title": self.message,
            "status": self.http_status,
            "class": self.problem_class,
            "reason_code": self.reason_code,
            "retryable": self.retryable,
        }
        if self.detail:
            problem["detail_fields"] = self.detail
        if instance is not None:
            problem["instance"] = instance
        return problem

    def to_dict(self) -> dict[str, Any]:
        """Flat form written to `job_events` / `jobs.rejection` (CONTRACT.md §3)."""
        return {
            "reason_code": self.reason_code,
            "detail": self.detail,
            "message": self.message,
        }


class ClinicalRejection(MedosError):
    """The job cannot be run on this data, and that is an ANSWER (CONTRACT.md §3).

    Maps to job state `REJECTED`, never `FAILED`. HTTP 422: the request was well formed
    and the server understood it; the *data* is outside the applicability envelope.
    Never retryable -- re-running produces the same rejection.
    """

    problem_class: ProblemClass = "clinical_rejection"
    http_status: int = 422
    retryable: bool = False


class TransportFailure(MedosError):
    """A network peer (DICOMweb / PACS / Orthanc) failed or misbehaved.

    Retryable by default: the study is fine, the hop was not. HTTP 502, because the fault
    is upstream of MedicalOS and the caller must not be told their request was bad.
    """

    problem_class: ProblemClass = "transport_failure"
    http_status: int = 502
    retryable: bool = True


class SystemFailure(MedosError):
    """A defect in MedicalOS, or an unmet invariant. HTTP 500, not retryable.

    Not retryable because retrying a bug re-runs the bug. The one thing this class MUST
    do is stay distinguishable from `ClinicalRejection` in metrics, or the error budget
    silently absorbs every rejection and the service looks healthier than it is.
    """

    problem_class: ProblemClass = "system_failure"
    http_status: int = 500
    retryable: bool = False


# MOS-IMG-010: closed enum. Adding a member is a minor version of the specification.
REJECTION_CODES: frozenset[str] = frozenset(
    {
        "geometry_missing_position",
        "geometry_unsupported_sop_class",
        "geometry_inconsistent_grid",
        "geometry_inconsistent_orientation",
        "geometry_duplicate_positions",
        "geometry_non_uniform_spacing",
        "geometry_gapped",
        "geometry_gantry_tilt",
        "geometry_tilt_correction_out_of_range",
        "geometry_unsupported_rescale",
        "geometry_insufficient_instances",
    }
)


class SeriesSelectionError(ClinicalRejection):
    """Nothing buildable was found under the given root.

    Distinct from GeometryRejection: this is "the selector had nothing to hand the
    builder", not one of the MOS-IMG-010 geometry codes. It is an ordinary exception
    (not SystemExit) so a batch caller can carry on to the next case - LCTSC-Test-S3-201,
    for instance, ships an RTSTRUCT with no CT series at all.
    """


class GeometryRejection(ClinicalRejection):
    """A clinical outcome, not a crash (MOS-IMG-011).

    Carries exactly one code from the MOS-IMG-010 closed enum plus the `detail` fields
    that row names. The job state this maps to is REJECTED, which every UI must render
    distinctly from FAILED - hence a dedicated exception type rather than ValueError.
    """

    def __init__(self, reason_code: str, detail: dict[str, Any], message: str) -> None:
        if reason_code not in REJECTION_CODES:
            raise ValueError(
                f"{reason_code!r} is not in the MOS-IMG-010 closed enum; "
                "adding a code is a spec minor version, not a code change"
            )
        super().__init__(f"[{reason_code}] {message}")
        self.reason_code = reason_code
        self.detail = detail
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "detail": self.detail,
            "message": self.message,
        }
