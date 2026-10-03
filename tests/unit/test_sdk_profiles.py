# SPDX-License-Identifier: Apache-2.0
"""`medos.sdk.profiles` + `python -m medos.sdk run` — the C3 surface.

The profile is the operator-editable half of a deployment; these tests pin the half
that bites operators: a CLOSED schema that refuses with the profile's own vocabulary
(`profile: pacs.kind is 'http'`, not `KeyError`), relative paths resolving against the
profile's directory, secrets coming from the environment rather than the file, and the
CLI's batch discipline — one refused study prints a JSON line and the batch continues.

Spec: roadmap C3; MOS-REL-108 (the profile is data; embedded inference loads the one
script the profile names, nothing else).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from medos.sdk.profiles import (
    Profile,
    ProfileError,
    build_worker,
    load_profile,
)

LOCAL_YAML = """
version: 1
card: selftest
pacs:
  kind: dicomweb
  base_url: "http://127.0.0.1:8043/dicomweb/tenant"
  token: dev-token
inference:
  kind: kserve_v2
  url: "http://127.0.0.1:8010"
select_series: ct_only
writer:
  kind: platform
mode: local
local:
  studies:
    - "1.2.3"
    - "4.5.6"
"""


def _write(tmp_path: Path, text: str, name: str = "profile.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_a_local_profile_loads(tmp_path: Path) -> None:
    profile = load_profile(_write(tmp_path, LOCAL_YAML))
    assert profile.mode == "local"
    assert profile.local_studies == ("1.2.3", "4.5.6")
    assert profile.card.model_id == "medos.selftest-affine"
    assert profile.select_kind == "ct_only"
    assert profile.writer is not None


def test_the_schema_is_closed(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="unknown key"):
        load_profile(_write(tmp_path, LOCAL_YAML + "extra: 1\n"))


def test_a_bad_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="version"):
        load_profile(_write(tmp_path, LOCAL_YAML.replace("version: 1", "version: 2")))


def test_a_bad_adapter_kind_is_refused_with_the_profiles_vocabulary(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="pacs.kind"):
        load_profile(
            _write(tmp_path, LOCAL_YAML.replace("kind: dicomweb", "kind: http"))
        )


def test_a_bad_mode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="mode"):
        load_profile(_write(tmp_path, LOCAL_YAML.replace("mode: local", "mode: batch")))


def test_the_secret_comes_from_the_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEDOS_TEST_TOKEN", "from-env")
    text = LOCAL_YAML.replace('token: dev-token', "token_env: MEDOS_TEST_TOKEN")
    profile = load_profile(_write(tmp_path, text))
    # DicomWebPacs carries the token lazily; the profile object is the seam we assert on.
    assert profile.raw["pacs"]["token_env"] == "MEDOS_TEST_TOKEN"
    assert "dev-token" not in text


def test_a_relative_card_resolves_against_the_profile_directory(tmp_path: Path) -> None:
    """`card: model` next to the profile is how an operator ships the model with the
    file that names it; an absolute-path-only answer would leak host layout into every
    profile."""
    from medos.sdk.modelcard import document_for

    card_dir = tmp_path / "model"
    bundle = card_dir / "bundle"
    bundle.mkdir(parents=True)
    from medos.sdk.fixtures import selftest_spec_document

    spec = selftest_spec_document()
    document = document_for(
        model_id=spec["model_id"],
        model_version=spec["model_version"],
        spec_document=spec,
        weights={"path": "bundle", "weights_file": "m.ts", "format": "torchscript",
                 "digest": "sha256:" + "0" * 64},
        frameworks={"torch": "t"},
        outputs=({"kind": "segmentation", "value": 1, "name": "x"},),
        stamp={"producer": "test"},
    )
    (card_dir / "modelcard.json").write_text(json.dumps(document), encoding="utf-8")

    text = LOCAL_YAML.replace("card: selftest", "card: model")
    profile = load_profile(_write(tmp_path, text))
    assert profile.card.directory == card_dir.resolve()


def test_embedded_inference_loads_the_named_script(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    script.write_text("def run(arrays, spacing):\n    return arrays\n", encoding="utf-8")
    text = LOCAL_YAML.replace(
        "inference:\n  kind: kserve_v2\n  url: \"http://127.0.0.1:8010\"",
        "inference:\n  kind: embedded\n  script: model.py",
    )
    profile = load_profile(_write(tmp_path, text))
    worker = build_worker(profile)
    assert worker.pipeline.inference is not None


def test_an_external_profile_normalises_the_mappings(tmp_path: Path) -> None:
    text = """
version: 1
card: selftest
pacs: {kind: dicomweb, base_url: "http://pacs/dicomweb/t", token: t}
inference: {kind: kserve_v2, url: "http://triton:8010"}
mode: external
external:
  bus: {kind: kafka, bootstrap_servers: "kafka:9092"}
  inbound: {topic: IN, fields: {study_uid: sid}}
  outbound: {topic: OUT, template: {type: R}}
  error: {topic: ERR, template: {type: E}}
"""
    profile = load_profile(_write(tmp_path, text))
    assert profile.mode == "external"
    assert profile.external["inbound"]["fields"] == {"study_uid": "sid"}


def test_cli_run_walks_local_studies_and_continues_past_a_refusal(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """One refused study prints a JSON line and the batch proceeds — a local run that
    loses its second half to its first exception is a debugging session, not a run."""
    from medos.sdk import __main__ as cli

    calls: list[str] = []

    class _Result:
        class findings:
            segments = (1,)
            measurements = ()
        stored = ()

    class _Worker:
        def submit(self, study_uid: str) -> _Result:
            calls.append(study_uid)
            if study_uid == "bad":
                raise RuntimeError("study refused: no eligible series")
            return _Result()

    profile = Profile(
        card=None, pacs=None, inference=None, writer=None,  # type: ignore[arg-type]
        mode="local", local_studies=("ok-1", "bad", "ok-2"), external=None,
        select_kind="image_only", raw={},
    )
    import medos.sdk.profiles as profiles_mod

    monkeypatch.setattr(profiles_mod, "load_profile", lambda _p: profile)
    monkeypatch.setattr(profiles_mod, "build_worker", lambda _p: _Worker())

    rc = cli.main(["run", "--profile", "whatever.yaml"])
    assert rc == 1, "a refusal in the batch is a non-zero exit, with the batch completed"
    assert calls == ["ok-1", "bad", "ok-2"]
    lines = [json.loads(x) for x in capsys.readouterr().out.strip().splitlines()]
    assert lines[0]["study"] == "ok-1" and "segments" in lines[0]
    assert lines[1]["study"] == "bad" and "refused" in lines[1]
    assert lines[2]["study"] == "ok-2"


def test_cli_refuses_a_broken_profile_without_a_traceback(
    tmp_path: Path, capsys
) -> None:
    from medos.sdk import __main__ as cli

    rc = cli.main(["run", "--profile", str(_write(tmp_path, "version: 2\n"))])
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip())
    assert "refused" in out
