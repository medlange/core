# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the native-inference plane. No containers, no network.

Everything here is either an arithmetic rule from chapter 13 §13.10 or a refusal gate.
Both classes of rule are the kind that get quietly relaxed under delivery pressure, which
is exactly why they are pinned by a test rather than by a comment.

Spec: MOS-OPS-068, MOS-OPS-069, MOS-OPS-070, MOS-OPS-071, MOS-OPS-072, MOS-OPS-073,
MOS-OPS-074, MOS-OPS-078, MOS-OPS-079, MOS-OPS-080, MOS-OPS-083, MOS-OPS-085,
MOS-OPS-086, MOS-OPS-087, MOS-OPS-090, MOS-OPS-092, MOS-OPS-114, MOS-SVC-058.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from medos.inference.backend import ModelRef
from medos.inference.dispatch import (
    NativeBackendMissing,
    build_backend,
    native_model_of,
    run_capability,
)
from medos.inference.inprocess import InProcessCpuBackend
from medos.inference.repository import (
    ConfigRejected,
    ModelManifest,
    ModelRepositoryError,
    render_config_pbtxt,
    triton_model_name,
    validate_config_pbtxt,
    write_model_directory,
)
from medos.inference.residency import (
    ModelSlot,
    ResidencyLedger,
    ResidencySettings,
    Unsatisfiable,
)
from medos.inference.selftest import (
    SELFTEST_MODEL_ID,
    SELFTEST_VERSIONS,
    build_manifest,
    fixture_models,
    golden_input,
    reference_forward,
)
from medos.inference.tritond import ModelPin, load_pins, measure_device_memory

# =====================================================================================
# MOS-OPS-068 / 069 -- name derivation
# =====================================================================================


def test_triton_model_name_matches_the_specification_example() -> None:
    """§13.10.1 writes this example out; it is the contract, not an illustration."""
    assert (
        triton_model_name("pulmo.pleural-effusion", "3.2.1")
        == "pulmo_pleural-effusion__3_2_1"
    )


def test_two_versions_of_one_model_get_distinct_names() -> None:
    """MOS-OPS-068's stated purpose: canary and blue/green need both resident at once."""
    a = triton_model_name(SELFTEST_MODEL_ID, "1.0.0")
    b = triton_model_name(SELFTEST_MODEL_ID, "1.1.0")
    assert a != b and a.endswith("__1_0_0") and b.endswith("__1_1_0")


@pytest.mark.parametrize(
    "model_id, version",
    [
        ("pulmo.pleural-effusion", "3.2.1-rc.1"),  # '-' would alias onto 3_2_1_rc_1
        ("pulmo.pleural-effusion", "3.2"),
        ("Pulmo.Pleural", "1.0.0"),  # uppercase: not a lowercase dotted identifier
        ("nodots", "1.0.0"),  # no namespace segment
        ("pulmo..x", "1.0.0"),
        ("../escape", "1.0.0"),  # would escape the model repository root
    ],
)
def test_model_ref_refuses_names_that_would_alias_or_escape(
    model_id: str, version: str
) -> None:
    with pytest.raises(ValueError):
        ModelRef(model_id=model_id, model_version=version)


# =====================================================================================
# MOS-OPS-071 / 072 / 073 / 074 -- the config.pbtxt refusal gates
# =====================================================================================
_BASE_CONFIG = """
name: "m__1_0_0"
platform: "onnxruntime_onnx"
max_batch_size: 0
input [ { name: "INPUT__0", data_type: TYPE_FP32, dims: [ -1, 1, 16, 16, 16 ] } ]
output [ { name: "OUTPUT__0", data_type: TYPE_FP32, dims: [ -1, 1, 16, 16, 16 ] } ]
instance_group [ { kind: KIND_CPU, count: 1 } ]
"""


def test_a_clean_config_passes() -> None:
    validate_config_pbtxt(_BASE_CONFIG)


def test_execution_accelerators_is_refused_MOS_OPS_071() -> None:
    """A load-time TensorRT build is a backend conversion: new numerics, new ModelVersion,
    its own EvaluationRun. Triton "MUST be configured so it cannot perform such a
    conversion at load time", so the stanza is refused at registration."""
    config = _BASE_CONFIG + "\noptimization { execution_accelerators { } }\n"
    with pytest.raises(ConfigRejected) as exc:
        validate_config_pbtxt(config)
    assert exc.value.rule == "MOS-OPS-071"


def test_response_cache_is_refused_MOS_OPS_074() -> None:
    """"A cache keyed on input tensor bytes can return one patient's mask for another
    patient's identical-looking patch." Forbidden outright, per-model enablement included."""
    with pytest.raises(ConfigRejected) as exc:
        validate_config_pbtxt(_BASE_CONFIG + "\nresponse_cache { enable: true }\n")
    assert exc.value.rule == "MOS-OPS-074"


def test_dynamic_batching_is_refused_MOS_OPS_072() -> None:
    with pytest.raises(ConfigRejected) as exc:
        validate_config_pbtxt(
            _BASE_CONFIG + "\ndynamic_batching { max_queue_delay_microseconds: 100 }\n"
        )
    assert exc.value.rule == "MOS-OPS-072"


def test_max_batch_size_must_be_zero_MOS_OPS_072() -> None:
    with pytest.raises(ConfigRejected) as exc:
        validate_config_pbtxt(_BASE_CONFIG.replace("max_batch_size: 0", "max_batch_size: 8"))
    assert exc.value.rule == "MOS-OPS-072"


def test_instance_count_above_one_is_refused_MOS_OPS_073() -> None:
    with pytest.raises(ConfigRejected) as exc:
        validate_config_pbtxt(_BASE_CONFIG.replace("count: 1", "count: 2"))
    assert exc.value.rule == "MOS-OPS-073"


def test_a_commented_out_stanza_does_not_trip_the_gate() -> None:
    """The gate is textual, so it has to ignore comments or it fails closed on prose."""
    validate_config_pbtxt(_BASE_CONFIG + "\n# response_cache { enable: true }\n")


def test_generated_config_passes_its_own_gates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Generation and validation are both applied, never either/or."""
    manifest = build_manifest("1.0.0", b"x" * 269)
    config = render_config_pbtxt(manifest, warmup_filename="golden.bin", warmup_batch=2)
    validate_config_pbtxt(config)
    assert 'platform: "onnxruntime_onnx"' in config
    assert "max_batch_size: 0" in config
    assert "version_policy { specific { versions: [ 1 ] } }" in config  # MOS-OPS-069
    assert "model_warmup" in config  # MOS-OPS-075


def test_render_refuses_instance_count_above_one() -> None:
    manifest = build_manifest("1.0.0", b"x" * 269)
    with pytest.raises(ModelRepositoryError, match="MOS-OPS-073"):
        render_config_pbtxt(manifest, instance_count=2)


def test_write_model_directory_produces_the_1310_1_layout(tmp_path) -> None:  # type: ignore[no-untyped-def]
    manifest = build_manifest("1.0.0", b"x" * 269)
    directory = write_model_directory(
        tmp_path, manifest, b"x" * 269, warmup_bytes=b"\x00" * 16, warmup_filename="g.bin"
    )
    assert directory.name == triton_model_name(SELFTEST_MODEL_ID, "1.0.0")
    # MOS-OPS-069: the version directory is always `1`.
    assert (directory / "1" / "model.onnx").exists()
    assert (directory / "config.pbtxt").exists()
    assert (directory / "medicalos.json").exists()
    assert (directory / "warmup" / "g.bin").exists()


# =====================================================================================
# MOS-OPS-070 / 078 -- the manifest and the footprint arithmetic
# =====================================================================================


def test_manifest_rejects_a_missing_field() -> None:
    with pytest.raises(ModelRepositoryError, match="missing"):
        ModelManifest.from_json({"model_id": "a.b"})


def test_manifest_rejects_a_digest_without_its_algorithm() -> None:
    manifest = build_manifest("1.0.0", b"x")
    raw = {**_manifest_dict(manifest), "artifact_digest": "deadbeef"}
    with pytest.raises(ModelRepositoryError, match="sha256"):
        ModelManifest.from_json(raw)


def _manifest_dict(manifest: ModelManifest) -> dict[str, object]:
    import json

    return json.loads(manifest.to_json())  # type: ignore[no-any-return]


def test_footprint_is_weights_plus_workspace_times_count() -> None:
    """§13.10.3: footprint_bytes(m) = weights + workspace[patch_batch] * instance_count."""
    manifest = build_manifest("1.0.0", b"x" * 269)
    workspace = manifest.workspace_bytes_by_patch_batch["2"]
    assert manifest.footprint_bytes(2) == 269 + workspace
    assert manifest.footprint_bytes(2, instance_count=2) == 269 + 2 * workspace


def test_footprint_refuses_a_patch_batch_outside_the_allowed_list_MOS_OPS_078() -> None:
    """The bit-identity assertion was only made for the listed values, so a value outside
    them is a revalidation question, not a capacity knob."""
    manifest = build_manifest("1.0.0", b"x" * 269)
    with pytest.raises(ModelRepositoryError, match="MOS-OPS-078"):
        manifest.footprint_bytes(3)


def test_selftest_output_hash_is_reproducible_from_the_manifest_alone() -> None:
    """MOS-OPS-015's comparison value has to be derivable from what is published, or the
    check can only ever be run by whoever built the model."""
    import hashlib

    manifest = build_manifest("1.0.0", b"x" * 269)
    spec = manifest.selftest
    regenerated = golden_input(int(spec["batch"]), int(spec["input_seed"]))
    expected = reference_forward(regenerated, SELFTEST_VERSIONS["1.0.0"])
    digest = "sha256:" + hashlib.sha256(np.ascontiguousarray(expected).tobytes()).hexdigest()
    assert digest == spec["output_sha256"]


def test_batch_independence_of_the_oracle_MOS_OPS_078() -> None:
    """The packaging-time half of MOS-OPS-078. The server-side half is in the integration
    suite, because an assertion made only against the oracle is about the oracle."""
    params = SELFTEST_VERSIONS["1.0.0"]
    one = reference_forward(golden_input(1), params)
    for batch in (1, 2, 4):
        stacked = np.concatenate([golden_input(1)] * batch, axis=0)
        out = reference_forward(stacked, params)
        for index in range(batch):
            assert np.array_equal(out[index], one[0])


# =====================================================================================
# §13.10.3 / §13.10.4 -- the residency contract
# =====================================================================================
GIB = 1024**3


def _ledger(
    budget_total: int = 16 * GIB,
    reserved: int = 1_288_490_188,
    ratio: float = 0.90,
    hold: int = 300,
    clock: datetime | None = None,
):  # type: ignore[no-untyped-def]
    loaded: list[str] = []
    unloaded: list[str] = []
    now = {"t": clock or datetime(2026, 9, 14, 12, 0, tzinfo=UTC)}

    settings = ResidencySettings(
        runtime_reserved_bytes=reserved,
        device_total_bytes=budget_total,
        headroom_ratio=ratio,
        min_hold_seconds=hold,
    )
    ledger = ResidencyLedger(
        settings,
        load=lambda ref: (loaded.append(str(ref)), 0.5)[1],
        unload=lambda ref: unloaded.append(str(ref)),
        clock=lambda: now["t"],
    )
    return ledger, loaded, unloaded, now


def test_budget_matches_the_worked_example_of_1310_4() -> None:
    """§13.10.4's T4 case, with a SPECIFICATION ARITHMETIC ERROR pinned here.

    §13.10.4 writes:

        budget_bytes = (17179869184 - 1288490188) * 0.90 = 14302841096 ≈ 13.32 GiB

    The stated product is wrong. 17179869184 - 1288490188 = 15891378996, and
    15891378996 * 0.90 = 14302241096.4, not 14302841096 -- the spec's figure is 600 000 B
    high. The approximation (≈ 13.32 GiB) and every conclusion drawn from it in
    §13.10.4 are unaffected: the three-model total of 15.55 GiB still exceeds the budget
    and any two still fit, by margins of gigabytes. The formula is implemented as written
    and this test asserts the arithmetic, not the typo. REPORTED, not silently matched.
    """
    settings = ResidencySettings(
        runtime_reserved_bytes=1_288_490_188,
        device_total_bytes=17_179_869_184,
        headroom_ratio=0.90,
    )
    assert settings.budget_bytes == 14_302_241_096
    assert abs(settings.budget_bytes / GIB - 13.32) < 0.01  # the spec's ≈ figure holds


def test_headroom_above_095_is_refused_MOS_OPS_080() -> None:
    with pytest.raises(ValueError, match="MOS-OPS-080"):
        ResidencySettings(
            runtime_reserved_bytes=0, device_total_bytes=GIB, headroom_ratio=0.99
        )


def test_three_capabilities_two_slots_nothing_fails_MOS_OPS_085() -> None:
    """§13.10.4's case, with §13.10.4's numbers. Any two fit; all three do not.

    The assertion is MOS-OPS-085's: "Nothing fails. Jobs for the non-resident capability
    remain QUEUED." So the third reservation is `queued`, not an error and not a refusal.
    """
    ledger, loaded, unloaded, _ = _ledger(budget_total=17_179_869_184, hold=300)
    sizes = {  # §13.10.4's table, in bytes
        "pulmo.lung-seg": int(3.25 * GIB),
        "pulmo.pleural-effusion": int(3.65 * GIB),
        "lidc.nodule-det": int(8.65 * GIB),
    }
    for model_id, footprint in sizes.items():
        ledger.register(
            ModelSlot(
                ref=ModelRef(model_id=f"{model_id}", model_version="1.0.0"),
                residency="on_demand",
                footprint_bytes=footprint,
                service_ids=(f"svc_{model_id.split('.')[1]}",),
            )
        )
    a = ledger.reserve(ModelRef("pulmo.lung-seg", "1.0.0"), "job_a", 1, 600)
    b = ledger.reserve(ModelRef("pulmo.pleural-effusion", "1.0.0"), "job_b", 1, 600)
    c = ledger.reserve(ModelRef("lidc.nodule-det", "1.0.0"), "job_c", 1, 600)
    assert a.state == "granted" and b.state == "granted"
    assert c.state == "queued" and c.reason == "gpu_memory_contention"
    assert c.retry_after_seconds and c.retry_after_seconds > 0
    # MOS-OPS-081: the starved service is NOT admissible, so its worker does not claim.
    assert "svc_nodule-det" not in ledger.admissible_service_ids()
    assert len(loaded) == 2 and unloaded == []


def test_eviction_exhausts_evictable_before_on_demand_MOS_OPS_114() -> None:
    ledger, loaded, unloaded, now = _ledger(budget_total=4 * GIB, reserved=0, hold=300)
    for name, residency in (("a.evictable", "evictable"), ("a.ondemand", "on_demand")):
        ledger.register(
            ModelSlot(
                ref=ModelRef(name, "1.0.0"), residency=residency, footprint_bytes=GIB
            )
        )
    ledger.register(
        ModelSlot(ref=ModelRef("a.newcomer", "1.0.0"), residency="on_demand",
                  footprint_bytes=2 * GIB)
    )
    for name in ("a.evictable", "a.ondemand"):
        outcome = ledger.reserve(ModelRef(name, "1.0.0"), f"job_{name}", 1, 60)
        assert outcome.state == "granted"
        ledger.release(outcome.reservation_id or "")
    # Both loaded, 2 GiB committed of a 3.6 GiB budget. The newcomer needs 2 GiB, so
    # exactly one victim must go -- and MOS-OPS-114 says it must be the evictable one,
    # even though the on_demand model is the less recently used of the two.
    now["t"] += timedelta(seconds=1)
    outcome = ledger.reserve(ModelRef("a.newcomer", "1.0.0"), "job_new", 1, 60)
    assert outcome.state == "granted"
    assert unloaded == ["a.evictable@1.0.0"]


def test_on_demand_inside_its_hold_window_is_not_a_victim_MOS_OPS_087() -> None:
    """"Without the hold window, three capabilities on two slots thrash." """
    ledger, _loaded, unloaded, now = _ledger(budget_total=3 * GIB, reserved=0, hold=300)
    ledger.register(
        ModelSlot(ref=ModelRef("a.incumbent", "1.0.0"), residency="on_demand",
                  footprint_bytes=2 * GIB)
    )
    ledger.register(
        ModelSlot(ref=ModelRef("a.newcomer", "1.0.0"), residency="on_demand",
                  footprint_bytes=2 * GIB)
    )
    first = ledger.reserve(ModelRef("a.incumbent", "1.0.0"), "job_1", 1, 60)
    ledger.release(first.reservation_id or "")
    now["t"] += timedelta(seconds=5)  # released, but inside the 300 s hold window
    queued = ledger.reserve(ModelRef("a.newcomer", "1.0.0"), "job_2", 1, 60)
    assert queued.state == "queued" and unloaded == []
    now["t"] += timedelta(seconds=400)  # hold window expired
    promoted = ledger.tick()
    assert promoted is not None and promoted.ref.model_id == "a.newcomer"
    assert unloaded == ["a.incumbent@1.0.0"]


def test_evictable_gets_no_hold_window_MOS_OPS_114() -> None:
    ledger, _loaded, _unloaded, now = _ledger(budget_total=4 * GIB, reserved=0, hold=300)
    ledger.register(
        ModelSlot(ref=ModelRef("a.evictable", "1.0.0"), residency="evictable",
                  footprint_bytes=GIB)
    )
    outcome = ledger.reserve(ModelRef("a.evictable", "1.0.0"), "job_1", 1, 60)
    slot = ledger.slot(ModelRef("a.evictable", "1.0.0"))
    assert slot is not None and slot.held_until == now["t"]  # zero-length window
    assert outcome.state == "granted"


def test_a_resident_model_is_never_evicted() -> None:
    """§13.10.3's table: `resident` "may be evicted: never" and "counts against budget:
    always". So when room must be made, the on_demand model goes and the pinned one does
    not, even though evicting the pinned one would also have freed enough."""
    ledger, _loaded, unloaded, _now = _ledger(budget_total=4 * GIB, reserved=0, hold=0)
    ledger.register(
        ModelSlot(ref=ModelRef("a.pinned", "1.0.0"), residency="resident",
                  footprint_bytes=GIB)
    )
    ledger.register(
        ModelSlot(ref=ModelRef("a.warm", "1.0.0"), residency="on_demand",
                  footprint_bytes=GIB)
    )
    ledger.register(
        ModelSlot(ref=ModelRef("a.newcomer", "1.0.0"), residency="on_demand",
                  footprint_bytes=2 * GIB)
    )
    warm = ledger.reserve(ModelRef("a.warm", "1.0.0"), "job_warm", 1, 60)
    ledger.release(warm.reservation_id or "")
    # budget 3.6 GiB; committed 1 (pinned) + 1 (warm) = 2. The newcomer's 2 GiB does not
    # fit, so exactly one victim is needed and only one is eligible.
    outcome = ledger.reserve(ModelRef("a.newcomer", "1.0.0"), "job_new", 1, 60)
    assert outcome.state == "granted"
    assert unloaded == ["a.warm@1.0.0"]
    pinned = ledger.slot(ModelRef("a.pinned", "1.0.0"))
    assert pinned is not None and pinned.residency == "resident"


def test_rotation_is_fifo_by_wait_time_and_cannot_starve_MOS_OPS_086() -> None:
    """`promote := argmax(waiting_since)` -- the LONGEST-waiting model wins, regardless of
    how many jobs a newer waiter has queued."""
    ledger, _loaded, _unloaded, now = _ledger(budget_total=3 * GIB, reserved=0, hold=0)
    for name in ("a.first", "a.second"):
        ledger.register(
            ModelSlot(ref=ModelRef(name, "1.0.0"), residency="on_demand",
                      footprint_bytes=2 * GIB)
        )
    ledger.register(
        ModelSlot(ref=ModelRef("a.holder", "1.0.0"), residency="on_demand",
                  footprint_bytes=2 * GIB)
    )
    # The holder KEEPS its reservation, so it is not an eviction candidate and the budget
    # is genuinely full: `active_reservations > 0` removes it from `_victims_locked`.
    held = ledger.reserve(ModelRef("a.holder", "1.0.0"), "job_h", 1, 600)
    assert held.state == "granted"
    assert ledger.reserve(ModelRef("a.first", "1.0.0"), "job_1", 1, 60).state == "queued"
    now["t"] += timedelta(seconds=10)
    second = ledger.reserve(ModelRef("a.second", "1.0.0"), "job_2a", 1, 60)
    assert second.state == "queued"
    ledger.reserve(ModelRef("a.second", "1.0.0"), "job_2b", 1, 60)  # more queued jobs
    ledger.release(held.reservation_id or "")
    now["t"] += timedelta(seconds=1)
    promoted = ledger.tick()
    assert promoted is not None and promoted.ref.model_id == "a.first"


def test_a_model_larger_than_the_budget_is_unsatisfiable_MOS_OPS_083() -> None:
    """409 `unsatisfiable`. MOS-OPS-083: job FAILED/'capacity_configuration', never
    REJECTED, "because REJECTED is a clinical outcome about the study"."""
    ledger, _l, _u, _n = _ledger(budget_total=2 * GIB, reserved=0)
    ledger.register(
        ModelSlot(ref=ModelRef("a.huge", "1.0.0"), residency="on_demand",
                  footprint_bytes=8 * GIB)
    )
    with pytest.raises(Unsatisfiable) as exc:
        ledger.reserve(ModelRef("a.huge", "1.0.0"), "job_x", 1, 60)
    body = exc.value.to_body()
    assert body["state"] == "unsatisfiable"
    assert body["reason"] == "footprint_exceeds_budget"


def test_registering_a_resident_model_over_budget_is_refused_MOS_OPS_084() -> None:
    ledger, _l, _u, _n = _ledger(budget_total=2 * GIB, reserved=0)
    with pytest.raises(Unsatisfiable):
        ledger.register(
            ModelSlot(ref=ModelRef("a.huge", "1.0.0"), residency="resident",
                      footprint_bytes=4 * GIB)
        )


def test_release_is_idempotent_MOS_OPS_082() -> None:
    ledger, _l, _u, _n = _ledger(budget_total=4 * GIB, reserved=0)
    ledger.register(
        ModelSlot(ref=ModelRef("a.m", "1.0.0"), residency="on_demand", footprint_bytes=GIB)
    )
    outcome = ledger.reserve(ModelRef("a.m", "1.0.0"), "job_1", 1, 60)
    assert ledger.release(outcome.reservation_id or "") is True
    assert ledger.release(outcome.reservation_id or "") is False  # harmless, per spec


def test_an_expired_reservation_is_reclaimed_by_ttl_MOS_OPS_082() -> None:
    ledger, _l, _u, now = _ledger(budget_total=4 * GIB, reserved=0)
    ledger.register(
        ModelSlot(ref=ModelRef("a.m", "1.0.0"), residency="on_demand", footprint_bytes=GIB)
    )
    ledger.reserve(ModelRef("a.m", "1.0.0"), "job_crashed", 1, 30)
    now["t"] += timedelta(seconds=31)
    state = ledger.state()
    assert state["resident"][0]["active_reservations"] == 0


def test_a_slow_load_on_a_non_resident_model_is_refused_MOS_OPS_092() -> None:
    """"A 30 s cold start inside the rotation converts contention into unbounded latency."""
    settings = ResidencySettings(runtime_reserved_bytes=0, device_total_bytes=4 * GIB)
    ledger = ResidencyLedger(
        settings, load=lambda ref: 45.0, unload=lambda ref: None
    )
    ledger.register(
        ModelSlot(ref=ModelRef("a.slow", "1.0.0"), residency="on_demand", footprint_bytes=GIB)
    )
    with pytest.raises(Unsatisfiable):
        ledger.reserve(ModelRef("a.slow", "1.0.0"), "job_1", 1, 60)


def test_state_reports_gpu_uuid_as_null_and_names_the_device() -> None:
    """A consumer keying on the field's presence must not read a CPU node as a GPU one."""
    ledger, _l, _u, _n = _ledger()
    state = ledger.state()
    assert "gpu_uuid" in state and state["gpu_uuid"] is None
    assert state["device"] == "cpu"


def test_nvidia_smi_reconciliation_is_not_stubbed_MOS_OPS_090() -> None:
    ledger, _l, _u, _n = _ledger()
    with pytest.raises(NotImplementedError, match="MOS-OPS-090"):
        ledger.reconcile_observed()


def test_runtime_reserved_bytes_is_measured_MOS_OPS_079() -> None:
    total, reserved, device = measure_device_memory()
    assert total > 0 and reserved >= 0 and reserved < total
    assert device == "cpu"


# =====================================================================================
# Pins -- resolution by id AND version
# =====================================================================================


def test_pins_parse_and_carry_residency_and_services() -> None:
    pins = load_pins(
        '[{"model_id":"medos.selftest-affine","model_version":"1.0.0",'
        '"residency":"resident","patch_batch_size":2,"service_ids":["svc_a"]}]'
    )
    assert pins == (
        ModelPin(
            ref=ModelRef(SELFTEST_MODEL_ID, "1.0.0"),
            residency="resident",
            patch_batch_size=2,
            service_ids=("svc_a",),
        ),
    )


def test_a_malformed_pin_raises_rather_than_being_skipped() -> None:
    """A tritond that comes up healthy while serving one fewer model than configured is
    the failure mode where a job waits forever for a slot that never existed."""
    with pytest.raises(ValueError):
        load_pins("{not json")


def test_a_pin_with_a_floating_version_is_impossible() -> None:
    with pytest.raises(ValueError):
        load_pins('[{"model_id":"medos.selftest-affine","model_version":"latest"}]')


# =====================================================================================
# The seam: dispatch, and the fixture that is NOT a driver
# =====================================================================================


def test_build_backend_never_returns_the_in_process_fixture(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """§15.2.9: the in-process loader "is NOT a shipped driver". A factory that could
    select it from configuration is a factory that will select it in production."""
    monkeypatch.delenv("MEDOS_TRITON_URL", raising=False)
    assert build_backend() is None
    monkeypatch.setenv("MEDOS_TRITON_URL", "http://triton:8000")
    backend = build_backend()
    assert backend is not None and backend.driver_name == "triton"


def test_the_fixture_backend_demands_a_written_reason() -> None:
    with pytest.raises(ValueError, match="not a shipped driver"):
        InProcessCpuBackend({}, fixture_reason="   ")


def test_the_fixture_backend_resolves_by_id_and_version() -> None:
    backend = InProcessCpuBackend(
        fixture_models(), fixture_reason="unit: oracle for the affine self-test model"
    )
    tensor = golden_input(1)
    assert backend.is_ready(SELFTEST_MODEL_ID, "1.0.0")
    assert not backend.is_ready(SELFTEST_MODEL_ID, "9.9.9")
    a = backend.infer(tensor, SELFTEST_MODEL_ID, "1.0.0")
    b = backend.infer(tensor, SELFTEST_MODEL_ID, "1.1.0")
    assert not np.array_equal(a, b)
    assert backend.driver_name.startswith("inprocess-fixture:")


class _Deterministic:
    """Stands in for the three real capabilities: pure, no model, no `native_model`."""

    capability_id = "unit_deterministic"
    version = "1.0.0"

    def __init__(self) -> None:
        self.ran = False

    def applicable(self, vol: object) -> None:
        return None

    def run(self, vol: object, ctx: object) -> object:
        from medos.core.bundle import CapabilityOutcome

        self.ran = True
        return CapabilityOutcome(
            capability_id=self.capability_id,
            findings=(),
            label_map=None,
            source_sop_instance_uids=(),
        )


class _NativeModelStub:
    model_id = SELFTEST_MODEL_ID
    model_version = "1.0.0"

    def preprocess(self, vol: object, ctx: object) -> np.ndarray:
        return golden_input(1)

    def postprocess(self, tensor: np.ndarray, vol: object, ctx: object) -> object:
        from medos.core.bundle import CapabilityOutcome

        assert tensor.shape == (1, 1, 16, 16, 16)
        return CapabilityOutcome(
            capability_id="unit_native",
            findings=(),
            label_map=None,
            source_sop_instance_uids=(),
        )


class _Native:
    capability_id = "unit_native"
    version = "1.0.0"
    native_model = _NativeModelStub()

    def applicable(self, vol: object) -> None:
        return None

    def run(self, vol: object, ctx: object) -> object:
        raise AssertionError(
            "run() must not be called for a native-mode capability; the dispatcher takes "
            "the preprocess -> backend -> postprocess path"
        )


def test_a_deterministic_capability_is_unaffected_by_the_seam() -> None:
    capability = _Deterministic()
    outcome = run_capability(capability, object(), object(), None)  # type: ignore[arg-type]
    assert capability.ran and outcome.capability_id == "unit_deterministic"
    assert native_model_of(capability) is None


def test_a_native_capability_is_routed_through_the_backend() -> None:
    backend = InProcessCpuBackend(
        fixture_models(), fixture_reason="unit: dispatch routing, no server required"
    )
    outcome = run_capability(_Native(), object(), object(), backend)  # type: ignore[arg-type]
    assert outcome.capability_id == "unit_native"
    assert backend.calls == 1


def test_a_native_capability_without_a_backend_raises_rather_than_falling_back() -> None:
    """There is deliberately no in-process fallback: it would serve a model no
    EvaluationRun covers (MOS-OPS-071), and MOS-OPS-083 makes this an operator error
    about the cluster rather than a clinical outcome about the study."""
    with pytest.raises(NativeBackendMissing) as exc:
        run_capability(_Native(), object(), object(), None)  # type: ignore[arg-type]
    assert exc.value.problem_class == "system_failure"
    assert exc.value.retryable is False
