#!/usr/bin/env python
# SPDX-License-Identifier: Apache-2.0
"""Run every registered capability over a real CT cohort and report what it produced.

This is the proof harness for `medos/medos/capabilities`. It is NOT a test: it makes no
assertion, it runs the capabilities exactly as `worker/steps.py` will (registry ->
`execution_order` -> `bind_dependencies` -> `run`) and it prints what came back,
including the rejections. The numbers in the capability module docstrings
(`lung_segmentation.KNOWN_FAILURE_MODES`, `emphysema_laa`'s measured offset) are this
script's output over `LCTSC-Test-*`; re-run it to regenerate them.

Where a case ships an RTSTRUCT whose FrameOfReferenceUID matches the selected CT
series, the same quantities are recomputed inside the `Lung_L`/`Lung_R` contours and
reported alongside. That reference is NOT ground truth for %LAA -- it is a DIFFERENT
mask (an anatomical contour rather than a Hounsfield threshold), and the point of
printing both is that the difference is the systematic offset LAA-FM-005 declares.

PHI: prints UIDs, never a name, an MRN or a date (CONTRACT.md §11). The case directory
name is a TCIA collection identifier, not a patient identifier.

Usage:
    python medos/tools/capability_cohort_report.py [--cases GLOB] [--limit N] [--json OUT]

Spec: MOS-IMG-039, MOS-IMG-040, MOS-IMG-041, MOS-IMG-119, MOS-SAFE-012, MOS-EXEC-001.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from medos.capabilities import (  # noqa: E402
    REGISTRY,
    CapabilityContext,
    CapabilityRejection,
    bind_dependencies,
    execution_order,
    load_concepts,
)
from medos.core.bundle import CapabilityOutcome  # noqa: E402
from medos.core.dicomio import scan_series, select_series  # noqa: E402
from medos.core.errors import MedosError  # noqa: E402
from medos.core.geometry import build_canonical_volume  # noqa: E402
from medos.core.masks import find_rtstruct, mask_from_rtstruct  # noqa: E402
from medos.core.measure import measure_laa_percent, measure_volume_ml  # noqa: E402

DEFAULT_ROOT = Path("F:/WorkSpace/PulmoAI/TCIA")
CAPABILITIES = ("lung_segmentation", "emphysema_laa", "pleural_effusion")
LAA_THRESHOLD_HU = -950.0


def dice(a: np.ndarray, b: np.ndarray) -> float:
    denom = int(a.sum()) + int(b.sum())
    return (2.0 * int((a & b).sum()) / denom) if denom else float("nan")


def reference_from_rtstruct(case: Path, vol: Any, src: Any) -> dict[str, Any] | None:
    """Recompute volume and %LAA inside the RTSTRUCT lung contours, or None."""
    try:
        rtstruct = find_rtstruct(case, vol.frame_of_reference_uid)
    # `medos.core.masks.find_rtstruct` signals "no matching RTSTRUCT" with SystemExit --
    # lifted spike behaviour, and SystemExit is a BaseException, so a bare `except
    # Exception` lets it tear the process down. Not every LCTSC case ships an RTSTRUCT
    # whose FrameOfReferenceUID matches the selected CT series (LCTSC-Test-S3-101 and
    # S3-201 do not), and the absence of a reference is not an error here.
    except (SystemExit, Exception):  # noqa: BLE001
        return None
    concepts = load_concepts()
    masks, _ = mask_from_rtstruct(
        rtstruct, vol, src, concepts.rtstruct_roi_map, roi_names=["Lung_L", "Lung_R"]
    )
    if "lung_left" not in masks or "lung_right" not in masks:
        return None
    total = masks["lung_left"] | masks["lung_right"]
    return {
        "masks": masks,
        "total": total,
        "volume_ml": {
            "lung_right": measure_volume_ml(masks["lung_right"], src).value,
            "lung_left": measure_volume_ml(masks["lung_left"], src).value,
            "lung": measure_volume_ml(total, src).value,
        },
        "laa_percent": {
            "lung_right": measure_laa_percent(
                masks["lung_right"], src, LAA_THRESHOLD_HU
            ).value,
            "lung_left": measure_laa_percent(
                masks["lung_left"], src, LAA_THRESHOLD_HU
            ).value,
            "lung": measure_laa_percent(total, src, LAA_THRESHOLD_HU).value,
        },
    }


def outcome_json(outcome: CapabilityOutcome) -> dict[str, Any]:
    return {
        "capability_id": outcome.capability_id,
        "label_map": (
            None
            if outcome.label_map is None
            else {
                "shape": list(outcome.label_map.array.shape),
                "dtype": str(outcome.label_map.array.dtype),
                "segments": [
                    f"{s.scheme}:{s.code} {s.meaning}" for s in outcome.label_map.segments
                ],
            }
        ),
        "n_source_instances": len(outcome.source_sop_instance_uids),
        "findings": [
            {
                "kind": f.kind,
                "present": f.present,
                "score": f.score,
                "measurements": [
                    {
                        "code": f"{m.name.scheme}:{m.name.code}",
                        "meaning": m.name.meaning,
                        "value": m.value,
                        "unit": m.unit,
                    }
                    for m in f.measurements
                ],
            }
            for f in outcome.findings
        ],
    }


def run_case(case: Path) -> dict[str, Any]:
    record: dict[str, Any] = {"case": case.name}
    started = time.monotonic()
    try:
        series_uid, paths = select_series(scan_series(case), None)
        vol, src, build_diag = build_canonical_volume(paths)
    except MedosError as exc:
        record.update(
            state="REJECTED",
            stage="series_selection_or_volume_build",
            reason_code=exc.reason_code,
            problem_class=exc.problem_class,
            message=str(exc),
        )
        record["seconds"] = time.monotonic() - started
        return record
    except Exception as exc:  # noqa: BLE001
        record.update(
            state="ERROR",
            stage="series_selection_or_volume_build",
            message=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=4),
        )
        record["seconds"] = time.monotonic() - started
        return record

    record["series_instance_uid"] = series_uid
    record["study_instance_uid"] = vol.study_instance_uid
    record["shape"] = list(vol.shape)
    record["spacing_mm"] = [float(v) for v in vol.spacing_mm]
    record["convolution_kernel"] = src.convolution_kernel
    record["n_instances"] = len(src.sop_instance_uids)
    record["sort_evidence"] = build_diag.get("sort_order_evidence")

    ctx = CapabilityContext(
        job_id=f"job_{case.name}",
        series_instance_uid=vol.series_instance_uid,
        clinical_use_mode="research_only",
        source=src,
    )

    outcomes: dict[str, CapabilityOutcome] = {}
    attempted: list[str] = []
    record["capabilities"] = {}
    order = execution_order(CAPABILITIES)
    record["execution_order"] = list(order)

    for capability_id in order:
        entry: dict[str, Any] = {}
        try:
            capability = bind_dependencies(
                REGISTRY[capability_id], outcomes, attempted=attempted
            )
        except CapabilityRejection as exc:
            # An upstream capability rejected, so this one has no input. Clinical, not a
            # crash -- see `medos.capabilities.bind_dependencies`.
            attempted.append(capability_id)
            entry.update(
                state="REJECTED",
                stage="bind",
                reason_code=exc.reason_code,
                problem_class=exc.problem_class,
                message=str(exc),
                detail=exc.detail,
            )
            record["capabilities"][capability_id] = entry
            continue
        attempted.append(capability_id)
        reason = capability.applicable(vol)
        if reason is not None:
            entry.update(state="REJECTED", stage="applicable", reason_code=reason)
            record["capabilities"][capability_id] = entry
            continue
        try:
            outcome = capability.run(vol, ctx)
        except CapabilityRejection as exc:
            entry.update(
                state="REJECTED",
                stage="run",
                reason_code=exc.reason_code,
                problem_class=exc.problem_class,
                message=str(exc),
                detail=exc.detail,
            )
            record["capabilities"][capability_id] = entry
            continue
        except Exception as exc:  # noqa: BLE001
            entry.update(
                state="ERROR",
                stage="run",
                message=f"{type(exc).__name__}: {exc}",
                traceback=traceback.format_exc(limit=4),
            )
            record["capabilities"][capability_id] = entry
            continue
        outcomes[capability_id] = outcome
        entry.update(state="COMPLETED", outcome=outcome_json(outcome))
        record["capabilities"][capability_id] = entry

    record["state"] = (
        "COMPLETED" if "lung_segmentation" in outcomes else "REJECTED"
    )

    reference = reference_from_rtstruct(case, vol, src)
    if reference is not None:
        record["rtstruct_reference"] = {
            "volume_ml": reference["volume_ml"],
            "laa_percent": reference["laa_percent"],
        }
        if "lung_segmentation" in outcomes:
            label_map = outcomes["lung_segmentation"].label_map
            assert label_map is not None
            right = label_map.array == 1
            left = label_map.array == 2
            record["agreement"] = {
                "dice_lung": dice(right | left, reference["total"]),
                "dice_lung_right": dice(right, reference["masks"]["lung_right"]),
                "dice_lung_left": dice(left, reference["masks"]["lung_left"]),
            }
            seg_total = measure_volume_ml(right | left, src).value
            record["agreement"]["volume_ratio_lung"] = (
                seg_total / reference["volume_ml"]["lung"]
            )
            if "emphysema_laa" in outcomes:
                by_kind = {
                    f.kind: (f.measurements[0].value if f.measurements else None)
                    for f in outcomes["emphysema_laa"].findings
                }
                if by_kind.get("laa_950_lung") is not None:
                    record["agreement"]["laa_delta_pp_lung"] = (
                        by_kind["laa_950_lung"] - reference["laa_percent"]["lung"]
                    )
    record["seconds"] = time.monotonic() - started
    return record


def summarise(records: list[dict[str, Any]]) -> None:
    print()
    print("=" * 118)
    print("PER-CASE MEASUREMENTS  (lung_segmentation volumes in ml, emphysema_laa in %)")
    print("=" * 118)
    header = (
        f"{'case':<22}{'slices':>7}{'dS':>6}"
        f"{'vol_R':>10}{'vol_L':>10}{'vol_tot':>10}"
        f"{'LAA_R':>9}{'LAA_L':>9}{'LAA_tot':>9}"
        f"{'Dice':>7}{'dLAA':>8}"
    )
    print(header)
    print("-" * 118)
    for r in records:
        if r.get("state") != "COMPLETED":
            reason = (
                r.get("reason_code")
                or r.get("capabilities", {})
                .get("lung_segmentation", {})
                .get("reason_code")
                or r.get("message", "?")
            )
            print(f"{r['case']:<22}{'REJECTED':>20}  {reason}")
            continue
        seg = r["capabilities"]["lung_segmentation"]["outcome"]
        vols = {
            f["kind"]: f["measurements"][0]["value"]
            for f in seg["findings"]
            if f["measurements"]
        }
        laa_entry = r["capabilities"].get("emphysema_laa", {})
        laa: dict[str, float | None] = {}
        if laa_entry.get("state") == "COMPLETED":
            laa = {
                f["kind"]: (f["measurements"][0]["value"] if f["measurements"] else None)
                for f in laa_entry["outcome"]["findings"]
            }
        agreement = r.get("agreement", {})

        def fmt(v: Any, width: int, prec: int) -> str:
            return f"{v:>{width}.{prec}f}" if isinstance(v, float) else f"{'-':>{width}}"

        print(
            f"{r['case']:<22}{r['shape'][0]:>7}{r['spacing_mm'][0]:>6.2f}"
            f"{fmt(vols.get('lung_right'), 10, 2)}"
            f"{fmt(vols.get('lung_left'), 10, 2)}"
            f"{fmt(vols.get('lung'), 10, 2)}"
            f"{fmt(laa.get('laa_950_lung_right'), 9, 3)}"
            f"{fmt(laa.get('laa_950_lung_left'), 9, 3)}"
            f"{fmt(laa.get('laa_950_lung'), 9, 3)}"
            f"{fmt(agreement.get('dice_lung'), 7, 3)}"
            f"{fmt(agreement.get('laa_delta_pp_lung'), 8, 3)}"
        )

    done = [r for r in records if r.get("state") == "COMPLETED"]
    rejected = [r for r in records if r.get("state") == "REJECTED"]
    errored = [r for r in records if r.get("state") == "ERROR"]
    print("-" * 118)
    print(
        f"{len(done)} produced a result, {len(rejected)} REJECTED, "
        f"{len(errored)} ERROR, of {len(records)} cases"
    )

    def stats(values: list[float], label: str) -> None:
        if not values:
            return
        arr = np.array(values, dtype=float)
        print(
            f"  {label:<28} n={arr.size:<3} min {arr.min():.3f}  "
            f"median {float(np.median(arr)):.3f}  max {arr.max():.3f}  "
            f"mean {arr.mean():.3f}"
        )

    print()
    print("AGREEMENT WITH THE RTSTRUCT Lung_L/Lung_R CONTOURS (cases that produced both)")
    for key, label in (
        ("dice_lung", "Dice, whole lung"),
        ("dice_lung_right", "Dice, right lung"),
        ("dice_lung_left", "Dice, left lung"),
        ("volume_ratio_lung", "volume / RTSTRUCT"),
        ("laa_delta_pp_lung", "%LAA delta (pp), LAA-FM-005"),
    ):
        stats([r["agreement"][key] for r in done if key in r.get("agreement", {})], label)

    print()
    print("pleural_effusion (placeholder -- CONTRACT.md §7):")
    pe_shapes: dict[str, int] = {}
    for r in records:
        entry = r.get("capabilities", {}).get("pleural_effusion")
        if not entry:
            continue
        if entry.get("state") != "COMPLETED":
            key = f"{entry.get('state')}:{entry.get('reason_code')}"
        else:
            findings = entry["outcome"]["findings"]
            key = " | ".join(
                f"kind={f['kind']} present={f['present']} score={f['score']} "
                f"n_measurements={len(f['measurements'])} "
                f"label_map={entry['outcome']['label_map']}"
                for f in findings
            )
        pe_shapes[key] = pe_shapes.get(key, 0) + 1
    for key, count in sorted(pe_shapes.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>3} case(s): {key}")

    if errored:
        print()
        print("ERRORS (not rejections -- these are defects):")
        for r in errored:
            print(f"  {r['case']}: {r.get('message')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--cases", default="LCTSC-Test-*")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    cases = sorted(p for p in args.root.glob(args.cases) if p.is_dir())
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print(f"no cases matched {args.root / args.cases}", file=sys.stderr)
        return 2

    records: list[dict[str, Any]] = []
    for case in cases:
        record = run_case(case)
        records.append(record)
        print(
            f"[{len(records):>2}/{len(cases)}] {case.name:<22} "
            f"{record.get('state', '?'):<10} {record['seconds']:6.1f}s",
            flush=True,
        )

    summarise(records)
    if args.json:
        args.json.write_text(
            json.dumps(records, indent=2, default=str), encoding="utf-8"
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
