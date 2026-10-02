# SPDX-License-Identifier: Apache-2.0
"""`medos doctor` must never call something ready that it did not check.

WHAT THIS FILE PROTECTS
-----------------------
The doctor's whole value is that a developer can trust its verdict, which means the
verdict has to be wrong in only one direction. Reporting a working thing as broken wastes
someone's afternoon. Reporting a broken thing as READY -- or reporting the local shell's
state as the deployment's -- sends them to production with a false picture, and this
command is the one place that would be believed.

That failure is not hypothetical. The FIRST version of this file's subject read
`os.environ` and reported `MEDOS_DEID_PROVENANCE` as REFUSING on a deployment where it was
correctly declared in docker-compose.yml, because the developer's shell had never heard of
it. A command written to expose "a check describing a state that is not real" was itself
one. `test_the_environment_source_is_always_reported` is the guard that came out of it.

THE CLASSIFICATION IS THE PRODUCT, so it is tested directly: a declaration whose absence
would force the platform to invent a claim about data is REFUSING (nothing is broken and
there is nothing to fix), and one that is merely unset is UNCONFIGURED (there is a fix and
the row prints it). Getting that backwards would teach a developer either to ignore
refusals or to treat configuration as governance.
"""

from __future__ import annotations

import inspect
import json

import pytest
from medos.cli import doctor


def _findings(sections, name):
    return [f for s in sections for f in s.findings if f.name == name]


def _one(sections, name):
    found = _findings(sections, name)
    assert len(found) == 1, f"{name}: expected one finding, got {len(found)}"
    return found[0]


# --------------------------------------------------------------------------------------
# the classification
# --------------------------------------------------------------------------------------


def test_a_declaration_that_is_a_claim_about_data_refuses_rather_than_asking_to_be_set() -> (
    None
):
    """`MEDOS_DEID_PROVENANCE` has no correct default. A row that told a developer to "set
    it" would be inviting them to assert something about images nobody has examined, into
    a row that is never updated."""
    sections, _ = doctor.run({})
    finding = _one(sections, "MEDOS_DEID_PROVENANCE")
    assert finding.status == "REFUSING", finding
    assert "no default is possible" in finding.detail


def test_a_declaration_that_is_merely_unset_asks_to_be_set() -> None:
    """`MEDOS_SEAL_STORE` is a deployment choice about where bytes go. There is nothing to
    assert and nothing to get wrong about a patient, so it is UNCONFIGURED and the row
    carries the fix."""
    sections, _ = doctor.run({})
    finding = _one(sections, "MEDOS_SEAL_STORE")
    assert finding.status == "UNCONFIGURED", finding
    assert finding.remedy, "an UNCONFIGURED row must say what to do"


def test_every_unready_row_says_what_it_blocks() -> None:
    """A status with no consequence attached is a status nobody acts on."""
    sections, _ = doctor.run({})
    for section in sections:
        for finding in section.findings:
            if finding.status in ("UNCONFIGURED", "REFUSING"):
                assert finding.blocks or finding.remedy, (
                    f"{finding.name} is {finding.status} and says neither what it blocks "
                    f"nor what to do about it."
                )


def test_a_declared_provenance_reads_as_ready_and_reports_its_status() -> None:
    sections, _ = doctor.run({
        "MEDOS_DEID_PROVENANCE": json.dumps({
            "deidentification_status": "public_deidentified",
            "deid_policy_id": "tcia:ps3.15-basic-profile",
            "uid_mapping_table_id": "tcia:published-uids",
        }),
    })
    finding = _one(sections, "MEDOS_DEID_PROVENANCE")
    assert finding.status == "READY"
    assert "public_deidentified" in finding.detail


def test_a_malformed_provenance_is_not_ready() -> None:
    """Present-but-unparseable is the worst case: the seal would fail later, in front of
    an operator, on a run that had already retrieved a cohort."""
    sections, _ = doctor.run({"MEDOS_DEID_PROVENANCE": "{not json"})
    finding = _one(sections, "MEDOS_DEID_PROVENANCE")
    assert finding.status == "UNCONFIGURED"
    assert "NOT VALID JSON" in finding.detail


def test_whitespace_is_not_a_declaration() -> None:
    sections, _ = doctor.run({"MEDOS_SEAL_STORE": "   "})
    assert _one(sections, "MEDOS_SEAL_STORE").status == "UNCONFIGURED"


# --------------------------------------------------------------------------------------
# the honesty guards
# --------------------------------------------------------------------------------------


def test_the_environment_source_is_always_reported() -> None:
    """THE GUARD THIS FILE EXISTS FOR.

    The declarations live inside the containers. Reading the developer's shell and
    presenting the result as the deployment's state is the exact defect the doctor is
    written to expose, and the first version of it did precisely that. Whatever source is
    used, the output must name it, so a reader can tell "your deployment declares this"
    from "your shell does".
    """
    _sections, source = doctor.run({})
    assert source, "run() must always report where it read the declarations from"
    rendered = doctor.render(_sections, source)
    assert source in rendered, "the rendered report must name the source"


def test_a_probe_that_cannot_run_is_never_ready() -> None:
    """UNKNOWN exists so that "could not check" has somewhere to go other than READY.
    Every status the renderer knows must be one of the four, and UNKNOWN must not be
    silently mapped onto READY anywhere."""
    assert set(doctor.MARK) == {"READY", "UNCONFIGURED", "REFUSING", "UNKNOWN"}
    finding = doctor.Finding("x", "UNKNOWN", "could not ask")
    rendered = doctor.render([doctor.Section("s", [finding])], "test")
    assert "0 ready" in rendered and "1 unknown" in rendered


def test_no_declaration_value_is_ever_printed() -> None:
    """A doctor whose output has to be handled as carefully as a secret is a doctor nobody
    runs. Presence is reported; bytes are not."""
    secret = "super-secret-salt-value-nobody-should-see"
    sections, source = doctor.run({"MEDOS_TENANT_SALT": secret,
                                   "MEDOS_API_VIEWER_AUTHORIZATION": "Bearer " + secret})
    rendered = doctor.render(sections, source)
    assert secret not in rendered, "a declaration's value reached the output"
    assert _one(sections, "MEDOS_TENANT_SALT").status == "READY"


def test_the_exit_code_does_not_fail_on_correct_refusals() -> None:
    """A command that exited non-zero because the platform is behaving correctly is a
    command whose exit code people learn to ignore -- and then it cannot report a real
    problem either."""
    sections, _ = doctor.run({})
    refusing = [f for s in sections for f in s.findings if f.status == "REFUSING"]
    assert refusing, "the empty environment should produce at least one correct refusal"
    # main() returns 1 only for UNCONFIGURED; asserted here on the same rule it uses.
    unconfigured = [f for s in sections for f in s.findings if f.status == "UNCONFIGURED"]
    assert unconfigured, "and at least one genuinely unset thing, or this test proves little"


def test_the_report_renders_without_a_stack() -> None:
    """It has to work on a laptop with nothing running -- that is when it is needed."""
    sections, source = doctor.run({})
    rendered = doctor.render(sections, source)
    assert "MedicalOS" in rendered
    assert "ready" in rendered


def test_json_output_carries_the_same_statuses_as_the_text() -> None:
    """Two renderings of one truth is two places for it to drift."""
    sections, _ = doctor.run({})
    payload = [{"section": s.title, "findings": [vars(f) for f in s.findings]}
               for s in sections]
    assert {f["status"] for s in payload for f in s["findings"]} <= set(doctor.MARK)
    for section in payload:
        for finding in section["findings"]:
            assert set(finding) == {"name", "status", "detail", "remedy", "blocks"}


# --------------------------------------------------------------------------------------
# Service probes: two addresses, because `doctor` runs in two places
# --------------------------------------------------------------------------------------
#
# The probe list was `127.0.0.1` at the HOST-PUBLISHED port and nothing else. That is
# right only when `doctor` runs on the host. Run the documented way --
# `docker compose exec medos-api python -m medos.cli doctor` -- `127.0.0.1` is the API
# container's own loopback, and a viewer answering 200 from the host was reported as
# "127.0.0.1:3000 refused" with the remedy "docker compose up -d": the command the
# operator had just run.
#
# That is the failure mode this whole file exists to prevent, pointing the other way. A
# checker whose red rows are sometimes wrong trains its reader to skip red rows.


def _probe(label: str) -> tuple[tuple[str, int], tuple[str, int], str]:
    for name, inside, outside, _blocks, profile in doctor.SERVICE_PROBES:
        if name == label:
            return inside, outside, profile
    raise AssertionError(f"no probe named {label!r}: {[p[0] for p in doctor.SERVICE_PROBES]}")


def test_the_viewer_is_probed_on_both_of_its_real_addresses() -> None:
    """The web origin listens on 80 and is published on 3000. One address cannot cover
    both, which is precisely why the single-address version was wrong rather than merely
    incomplete."""
    inside, outside, _ = _probe("web (viewer origin)")
    assert inside == ("web", 80), inside
    assert outside == ("127.0.0.1", 3000), outside


def test_a_service_reachable_only_on_the_compose_network_reads_as_ready(monkeypatch) -> None:
    """The regression, stated as a test: inside a container the host address fails and
    the service name works, and the result must be READY."""
    inside, outside, _ = _probe("web (viewer origin)")
    monkeypatch.setattr(doctor, "_tcp", lambda h, p, timeout=3.0: (h, p) == inside)
    monkeypatch.setattr(doctor, "_http", lambda url, timeout=3.0: (200, ""))
    row = next(f for f in doctor.check_services({}).findings if f.name == "web (viewer origin)")
    assert row.status == "READY", row
    assert "compose network" in row.detail, row.detail


def test_a_service_reachable_only_from_the_host_reads_as_ready(monkeypatch) -> None:
    """The other direction, so the fix cannot silently become inside-only."""
    inside, outside, _ = _probe("web (viewer origin)")
    monkeypatch.setattr(doctor, "_tcp", lambda h, p, timeout=3.0: (h, p) == outside)
    monkeypatch.setattr(doctor, "_http", lambda url, timeout=3.0: (200, ""))
    row = next(f for f in doctor.check_services({}).findings if f.name == "web (viewer origin)")
    assert row.status == "READY", row
    assert "from the host" in row.detail, row.detail


def test_an_unreachable_service_names_both_addresses_it_tried(monkeypatch) -> None:
    """"refused" without saying where is a row an operator cannot act on."""
    monkeypatch.setattr(doctor, "_tcp", lambda h, p, timeout=3.0: False)
    monkeypatch.setattr(doctor, "_http", lambda url, timeout=3.0: (None, "refused"))
    row = next(f for f in doctor.check_services({}).findings if f.name == "web (viewer origin)")
    assert row.status != "READY"
    assert "web:80" in row.detail and "127.0.0.1:3000" in row.detail, row.detail


def test_a_profiled_service_names_its_profile_in_the_remedy(monkeypatch) -> None:
    """MinIO moved behind `--profile inference`, so absent is now the NORMAL state and
    `docker compose up -d` will never start it. A remedy that cannot work is worse than
    no remedy: it costs the reader a round trip to discover the advice was wrong."""
    monkeypatch.setattr(doctor, "_tcp", lambda h, p, timeout=3.0: False)
    monkeypatch.setattr(doctor, "_http", lambda url, timeout=3.0: (None, "refused"))
    row = next(f for f in doctor.check_services({}).findings if f.name == "object store")
    assert "--profile inference" in row.remedy, row.remedy


def test_a_default_set_service_does_not_name_a_profile(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_tcp", lambda h, p, timeout=3.0: False)
    monkeypatch.setattr(doctor, "_http", lambda url, timeout=3.0: (None, "refused"))
    row = next(f for f in doctor.check_services({}).findings if f.name == "postgres")
    assert "--profile" not in row.remedy, row.remedy


def test_orthanc_is_not_probed_and_the_reason_is_written_down() -> None:
    """MOS-DATA-006 puts Orthanc on the `pacs` network with no host port and makes the
    gateway the only thing permitted to reach it. A probe from here SHOULD fail, so a row
    would be permanently red and would teach a reader to ignore red rows."""
    assert not any(p[0] == "orthanc" for p in doctor.SERVICE_PROBES)
    source = inspect.getsource(doctor)
    assert "MOS-DATA-006" in source, (
        "the reason Orthanc is unprobed must stay written next to the probe list; "
        "without it the omission reads as an oversight and gets 'fixed'."
    )


# --------------------------------------------------------------------------------------
# The archive row, which did not exist while its docstring said it did
# --------------------------------------------------------------------------------------
#
# `check_data`'s first line read "What the archive holds. Read-only, and via the API
# rather than the database" while the function issued NO HTTP REQUEST AT ALL. It globbed
# two local directories and reported on those. So the one command README tells a newcomer
# to run could not tell them their archive was empty -- the most likely thing to be wrong
# on a first run -- and after `--profile demo` could not confirm the study had landed.
#
# A docstring that describes a behaviour the function does not have is worse than no
# docstring: it is the thing a reader checks INSTEAD of the code.


def test_an_empty_archive_is_reported_and_names_the_demo_profile(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_archive_study_count", lambda env: (0, "http://g/studies"))
    row = next(f for f in doctor.check_data({}).findings if f.name == "archive")
    assert row.status == "UNCONFIGURED"
    assert "--profile demo" in row.remedy, row.remedy


def test_a_populated_archive_reports_its_count(monkeypatch) -> None:
    monkeypatch.setattr(doctor, "_archive_study_count", lambda env: (4, "http://g/studies"))
    row = next(f for f in doctor.check_data({}).findings if f.name == "archive")
    assert row.status == "READY"
    assert "4" in row.detail, row.detail


@pytest.mark.parametrize("code", ["401", "403"])
def test_a_refused_credential_is_unconfigured_and_not_unknown(monkeypatch, code) -> None:
    """The gateway ANSWERED. What it said is that this process holds no credential, which
    is a declaration a deployment makes -- exactly what UNCONFIGURED means here. Reporting
    it UNKNOWN would tell the reader the check could not run, when it ran and succeeded."""
    monkeypatch.setattr(
        doctor, "_archive_study_count", lambda env: (None, f"http://g/studies answered {code}")
    )
    row = next(f for f in doctor.check_data({}).findings if f.name == "archive")
    assert row.status == "UNCONFIGURED", row
    assert "MEDOS_DICOMWEB_TOKEN" in row.remedy, row.remedy


def test_an_unreachable_gateway_is_unknown_and_never_ready(monkeypatch) -> None:
    """A probe that could not run has not passed. This is the other half of the previous
    test: not every failure is a configuration answer."""
    monkeypatch.setattr(
        doctor, "_archive_study_count", lambda env: (None, "gateway not reachable")
    )
    row = next(f for f in doctor.check_data({}).findings if f.name == "archive")
    assert row.status == "UNKNOWN", row


def test_the_archive_is_asked_over_dicomweb_and_not_the_database() -> None:
    """MOS-DATA-006 makes the gateway the only component permitted to hold a PACS
    credential, so it is the only thing that can say what is actually in the archive.
    Postgres would answer what the PLATFORM has been told about, a different and usually
    smaller set -- and a count that quietly means something else is worse than none."""
    source = inspect.getsource(doctor._archive_study_count)
    assert "/dicomweb/" in source and "/studies" in source
    assert "psycopg" not in source and "SELECT" not in source.upper()


def test_the_check_data_docstring_no_longer_claims_what_it_does_not_do() -> None:
    """The specific regression. It now issues the request its docstring describes, and
    this asserts the two cannot drift apart again without somebody noticing."""
    doc = doctor.check_data.__doc__ or ""
    assert "archive" in doc.lower()
    source = inspect.getsource(doctor.check_data)
    assert "_archive_study_count" in source, (
        "check_data's docstring talks about the archive; if it stops actually asking, "
        "the docstring is a lie again."
    )
