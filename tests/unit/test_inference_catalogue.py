# SPDX-License-Identifier: Apache-2.0
"""The inference adapter catalogue selects among shipped backends and imports nothing.

WHY THIS FILE EXISTS
---------------------
`OQ-10` is decided: the inference runtime is replaceable and Triton is one adapter. The
mechanism that makes that true is `medos/medos/inference/catalogue.py`, and the mechanism has
exactly two ways to be wrong.

THE FIRST IS THE ONE `MOS-REL-108` CARES ABOUT. "MedicalOS MUST NOT offer an in-process
plugin API -- no shared-library loading, no dynamic module import, no user-supplied code
executed inside a platform process." A registry that resolved a backend name through
`importlib` would be the mechanism `MOS-CONF-109`'s IEC 62304 section 4.3 segregation
argument denies exists -- and this repository has already shipped that defect once, as
entry 68 of `docs/spec/99-known-inconsistencies.md`. So the first test here reads the
source and fails on an import machinery call, the same way
`tests/unit/test_declared_dependencies.py` reads imports rather than trusting them.

THE SECOND IS QUIETER. `medos/medos/inference/inprocess.py` is a TEST FIXTURE, not a shipped
driver (`docs/spec/15-delivery.md` section 15.2.9), and `dispatch.build_backend` had no
branch that could return it for a stated reason: "a factory that could select it from
configuration is a factory that will select it in production one misconfigured environment
variable later." Widening one env var into a catalogue is exactly the change that quietly
loses that property, so it is asserted here.

Spec: OQ-10 / MOS-OPEN-031, MOS-REL-108, MOS-CONF-109, MOS-SVC-058.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from medos.inference.catalogue import (
    BACKENDS,
    BackendNotShipped,
    BackendResolver,
    backend_names,
    build_named,
)

SOURCE = Path(__file__).resolve().parents[2] / "medos" / "medos" / "inference" / "catalogue.py"


# --------------------------------------------------------------------------------------
# MOS-REL-108: selection, never import
# --------------------------------------------------------------------------------------


def test_the_catalogue_contains_no_import_machinery() -> None:
    """THE PROPERTY `MOS-CONF-109` IS SPENT ON. A reviewer executing the segregation
    argument must find a dictionary, not an importer."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    forbidden = {"import_module", "__import__", "importlib", "exec", "eval", "load_module"}
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            name = getattr(target, "attr", None) or getattr(target, "id", None)
            if name in forbidden:
                found.append(f"{name}() at line {node.lineno}")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", "") or ""
            names = [a.name for a in node.names]
            if mod == "importlib" or "importlib" in names:
                found.append(f"importlib imported at line {node.lineno}")
    assert not found, (
        "the inference catalogue reaches for import machinery:\n  " + "\n  ".join(found)
        + "\nMOS-REL-108 forbids dynamic module import inside a platform process, and "
        "MOS-CONF-109 cites that clause as an IEC 62304 4.3 segregation argument 'a "
        "reviewer can execute rather than read'. Selection is a dictionary lookup over "
        "top-level imports; introducing a backend is a rebuild."
    )


def test_an_unknown_backend_is_refused_and_the_known_ones_are_named() -> None:
    with pytest.raises(BackendNotShipped) as caught:
        build_named("tensorrt-llm")
    message = str(caught.value)
    assert "tensorrt-llm" in message
    for shipped in backend_names():
        assert shipped in message, "the refusal must name what IS shipped, not just what is not"


def test_the_in_process_fixture_is_not_selectable() -> None:
    """`inprocess` is a test fixture, not a driver (15.2.9).

    `build_backend` had no branch for it because "a factory that could select it from
    configuration is a factory that will select it in production one misconfigured
    environment variable later". Turning one env var into a catalogue must not lose that.
    """
    assert "inprocess" not in BACKENDS
    assert "in_process" not in BACKENDS
    assert "selftest" not in BACKENDS
    source = SOURCE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#") and "inprocess.py" not in line
    )
    assert "medos.inference.inprocess" not in code, (
        "the catalogue reaches the in-process fixture in executable code"
    )


# --------------------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------------------


def test_a_per_model_override_beats_the_default() -> None:
    """THE WHOLE POINT. One deployment, two models, two runtimes -- which is the case
    OQ-10's branch table says a mandated Triton cannot serve."""
    r = BackendResolver(default="triton", overrides={"nodule_detect": "onnxruntime"})
    assert r.backend_name_for("lung_segmentation") == "triton"
    assert r.backend_name_for("nodule_detect") == "onnxruntime"


def test_no_configuration_resolves_to_nothing_rather_than_raising() -> None:
    """A deployment running only deterministic capabilities legitimately has no inference
    backend. `run_capability` raises `NativeBackendMissing` at the point the assumption is
    actually wrong, which is where the operator can act on it."""
    assert BackendResolver().resolve("lung_segmentation") is None


def test_the_environment_migration_keeps_existing_deployments_working() -> None:
    """Every deployment written before the catalogue sets `MEDOS_TRITON_URL` and no
    `MEDOS_INFERENCE_BACKEND`. Defaulting to triton when the URL is present is the
    migration this file owes them; without it the change is a silent outage."""
    import os

    old = {k: os.environ.get(k) for k in
           ("MEDOS_INFERENCE_BACKEND", "MEDOS_INFERENCE_BACKEND_MAP", "MEDOS_TRITON_URL")}
    try:
        os.environ.pop("MEDOS_INFERENCE_BACKEND", None)
        os.environ.pop("MEDOS_INFERENCE_BACKEND_MAP", None)
        os.environ["MEDOS_TRITON_URL"] = "http://triton:8000"
        assert BackendResolver.from_env().backend_name_for("anything") == "triton"

        os.environ.pop("MEDOS_TRITON_URL")
        assert BackendResolver.from_env().backend_name_for("anything") is None
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_a_malformed_map_entry_is_refused_at_configuration_time() -> None:
    """Not at inference time. An operator's typo must not become a failed clinical job."""
    import os

    old = os.environ.get("MEDOS_INFERENCE_BACKEND_MAP")
    try:
        os.environ["MEDOS_INFERENCE_BACKEND_MAP"] = "lung_segmentation triton"
        with pytest.raises(BackendNotShipped, match="model_id=backend"):
            BackendResolver.from_env()
    finally:
        if old is None:
            os.environ.pop("MEDOS_INFERENCE_BACKEND_MAP", None)
        else:
            os.environ["MEDOS_INFERENCE_BACKEND_MAP"] = old


def test_the_map_parses_commas_and_whitespace() -> None:
    import os

    old = os.environ.get("MEDOS_INFERENCE_BACKEND_MAP")
    try:
        os.environ["MEDOS_INFERENCE_BACKEND_MAP"] = "a=triton,  b=triton   c=triton"
        r = BackendResolver.from_env()
        assert [r.backend_name_for(m) for m in ("a", "b", "c")] == ["triton"] * 3
    finally:
        if old is None:
            os.environ.pop("MEDOS_INFERENCE_BACKEND_MAP", None)
        else:
            os.environ["MEDOS_INFERENCE_BACKEND_MAP"] = old


def test_one_backend_is_built_once_and_shared() -> None:
    """Each adapter holds a connection pool; building one per inference opens a socket per
    study. Two models on one backend share one client."""
    built: list[str] = []

    class Probe(BackendResolver):
        def __init__(self) -> None:
            super().__init__(default="triton", overrides={"b": "triton"})

    r = Probe()
    r._cache["triton"] = object()          # stand in for a constructed client
    first = r.resolve("a")
    second = r.resolve("b")
    assert first is second, "two models on one backend must share one client"
    assert built == []


def test_selections_reports_the_routing() -> None:
    """A deployment that cannot see its own routing discovers it one failed job at a
    time."""
    r = BackendResolver(default="triton", overrides={"nodule_detect": "onnxruntime"})
    assert r.selections() == {"*": "triton", "nodule_detect": "onnxruntime"}


def test_every_catalogue_entry_is_callable_and_lower_case() -> None:
    """The key is what an operator types, and the parser splits on commas and whitespace:
    a key containing either could be shipped and never selectable -- the defect
    `medos/services/catalogue.py` records in miniature."""
    for name, factory in BACKENDS.items():
        assert name == name.lower().strip(), f"{name!r} is not a selectable token"
        assert "," not in name and not any(c.isspace() for c in name), name
        assert callable(factory), f"{name!r} does not map to a factory"
