# SPDX-License-Identifier: Apache-2.0
"""The serving pipeline end to end, on fakes: fetch -> chain -> infer -> postprocess.

The pipeline's promise is composition: the SAME card, pacs and inference adapters run in
the local mode and in the external (bus-driven) mode, and the only things a test has to
fake are the two seams. The canonical-volume builder is injected too, so this test needs
no DICOM on disk: the volume is synthesised at the selftest spec's own geometry, which is
exactly the grid the card's chain was built for.

What is asserted is the CONTRACT of the composition, not numpy arithmetic: the findings
carry the segments and measurements the card's outputs descriptor promised, the timings
are ordered and named the way both deployment modes report them, and a selector that
keeps nothing is a refusal, not an empty success.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from medos.sdk.adapters.inference import EmbeddedInference, ModelOutput
from medos.sdk.adapters.pacs import SeriesRef
from medos.sdk.fixtures import selftest_spec_document
from medos.sdk.modelcard import CARD_FILENAME, document_for
from medos.sdk.pipeline import Pipeline, PipelineError, StudyTask


class FakePacs:
    """One CT series with two instances; records what was fetched."""

    def __init__(self, study_uid: str = "1.2.3") -> None:
        self.study_uid = study_uid
        self.fetched: list[str] = []
        self.extra = SeriesRef(study_uid, "1.2.3.2", "MR", "not ct")

    def list_series(self, study_uid: str) -> list[SeriesRef]:
        assert study_uid == self.study_uid
        return [SeriesRef(study_uid, "1.2.3.1", "CT", "chest"), self.extra]

    def fetch_series(
        self, study_uid: str, series_uid: str, into: Path
    ) -> Sequence[Path]:
        into.mkdir(parents=True, exist_ok=True)
        files = []
        for n in (1, 2):
            path = into / f"{n}.dcm"
            path.write_bytes(b"fake instance bytes")
            files.append(path)
        self.fetched.append(series_uid)
        return files

    stored_study: str = ""

    def store(self, files: Sequence[Path], study_uid: str) -> None:
        self.stored_study = study_uid


def _card_root(tmp_path: Path) -> Path:
    spec_document = selftest_spec_document()
    # label_set[0] is background (value 0); the embedded model draws its blob at value 1,
    # which is the first real segment.
    label = spec_document["io"]["label_set"][1]
    root = tmp_path / "model"
    (root / "bundle").mkdir(parents=True)
    document = document_for(
        model_id=spec_document["model_id"],
        model_version=spec_document["model_version"],
        spec_document=spec_document,
        weights={"path": "bundle"},
        frameworks={"monai_bundle": "1.4.0"},
        outputs=(
            {"kind": "segmentation", "value": label["value"], "name": label["name"]},
            {"kind": "measurement", "name": "emphysema_percent",
             "from": "emphysema_percent", "unit": "%"},
        ),
        stamp={"code_commit": "d" * 40},
    )
    (root / CARD_FILENAME).write_text(json.dumps(document), encoding="utf-8")
    return root


def _volume_builder(spec_document: dict):
    patch = spec_document["patch"]["size_voxels"]
    spacing = tuple(spec_document["target_spacing_mm"])
    code = spec_document["orientation_target"]

    def build(paths):  # the fake ignores the fetched bytes entirely
        array = np.full((patch[0], patch[1], patch[2]), 100.0, dtype=np.float32)
        return SimpleNamespace(
            array=array, spacing_mm=spacing, anatomical_code=code
        ), None, {}

    return build


def _embedded_model(arrays, spacing) -> ModelOutput:
    image = arrays["image"][0]
    label_map = np.zeros(image.shape, dtype=np.int16)
    label_map[0:4, 0:4, 0:4] = 1
    return ModelOutput(
        arrays={"pred": label_map},
        metrics={"emphysema_percent": 12.5},
    )


def _pipeline(tmp_path: Path, **kwargs) -> tuple[Pipeline, FakePacs]:
    from medos.sdk.modelcard import ModelCard

    spec_document = selftest_spec_document()
    root = _card_root(tmp_path)
    card = ModelCard.load(root)
    pacs = FakePacs()
    pipeline = Pipeline(
        card,
        pacs=pacs,
        inference=EmbeddedInference(_embedded_model),
        volume_builder=_volume_builder(spec_document),
        **kwargs,
    )
    return pipeline, pacs


def test_a_study_runs_fetch_chain_infer_postprocess_in_order(tmp_path: Path) -> None:
    pipeline, pacs = _pipeline(tmp_path)

    result = pipeline.run_study(StudyTask(study_uid="1.2.3", task_id="task-1"))

    assert pacs.fetched == ["1.2.3.1", "1.2.3.2"] or set(pacs.fetched) == {
        "1.2.3.1", "1.2.3.2",
    }
    segment = result.findings.segments[0]
    assert segment.name == selftest_spec_document()["io"]["label_set"][1]["name"]
    assert int(segment.mask.sum()) == 64  # the 4x4x4 blob the embedded model drew
    measurement = result.findings.measurements[0]
    assert measurement.name == "emphysema_percent"
    assert measurement.value == 12.5
    assert measurement.unit == "%"
    assert result.download_started_at <= result.download_finished_at
    assert result.process_started_at >= result.download_finished_at
    assert result.process_finished_at >= result.process_started_at
    assert result.work_dir is None  # cleaned by default


def test_the_series_selector_narrows_what_gets_fetched(tmp_path: Path) -> None:
    pipeline, pacs = _pipeline(
        tmp_path, select_series=lambda refs: [r for r in refs if r.modality == "CT"]
    )

    result = pipeline.run_study("1.2.3")

    assert pacs.fetched == ["1.2.3.1"]
    assert [s.series_uid for s in result.series] == ["1.2.3.1"]


def test_a_selector_that_keeps_nothing_is_a_refusal(tmp_path: Path) -> None:
    pipeline, _ = _pipeline(tmp_path, select_series=lambda refs: [])

    with pytest.raises(PipelineError, match="kept none"):
        pipeline.run_study("1.2.3")


def test_keep_work_dir_leaves_the_fetched_instances_for_inspection(
    tmp_path: Path,
) -> None:
    pipeline, _ = _pipeline(tmp_path, keep_work_dir=True)

    result = pipeline.run_study("1.2.3")

    assert result.work_dir is not None
    assert list((result.work_dir / "1.2.3.1").glob("*.dcm"))


class FakeWriter:
    """Writes one result file; proves the write -> store -> record loop."""

    def __init__(self, pacs: FakePacs) -> None:
        self.pacs = pacs
        self.seen: list[str] = []

    def write(self, result, *, into):
        into.mkdir(parents=True, exist_ok=True)
        path = into / "result.dcm"
        path.write_bytes(b"seg-or-sr bytes")
        self.seen.append(result.task.study_uid)
        return [path]


def test_a_writer_closes_the_loop_write_store_record(tmp_path: Path) -> None:
    pipeline, pacs = _pipeline(tmp_path)
    pipeline.writer = FakeWriter(pacs)

    result = pipeline.run_study("1.2.3")

    assert len(result.stored) == 1
    assert pacs.stored_study == "1.2.3"
    assert result.stored[0].is_file()  # work dir kept because results were stored
    assert result.work_dir is not None


def test_the_default_selector_skips_derived_series(tmp_path: Path) -> None:
    from medos.sdk.adapters.pacs import SeriesRef
    from medos.sdk.pipeline import _image_series_only

    refs = [
        SeriesRef("s", "1", "CT"),
        SeriesRef("s", "2", "SEG"),
        SeriesRef("s", "3", "SR"),
        SeriesRef("s", "4", "MR"),
    ]
    assert [r.series_uid for r in _image_series_only(refs)] == ["1", "4"]
