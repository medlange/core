# SPDX-License-Identifier: Apache-2.0
"""The PlatformWriter driver's translation layer, tested where it is pure.

The full write -- SEG/SR bytes off real source datasets -- is integration territory
(exercised against the live stack in the SDK e2e script); what is pure, and what a
wrong change would break silently, is the translation: card descriptors to coded
concepts THROUGH THE PLATFORM DICTIONARY (never invented, MOS-IMG-112), model output
to the writer's LabelMap with the source-grid check, and the refusal vocabulary.
Each refusal is a behaviour, asserted as one.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from medos.sdk.adapters.results import (
    WriterRefused,
    _findings,
    _label_map,
    _requested_outputs,
    _segment_concepts,
)


@pytest.fixture()
def concepts():
    import medos.capabilities as caps

    return caps.load_concepts()


def _card(outputs):
    return SimpleNamespace(model_id="test.model", outputs=outputs)


SEG_RIGHT = {"kind": "segmentation", "value": 1, "name": "right", "structure": "lung_right"}
SEG_LEFT = {"kind": "segmentation", "value": 2, "name": "left", "structure": "lung_left"}


def test_segment_concepts_resolve_through_the_dictionary(concepts) -> None:
    coded = _segment_concepts(_card([SEG_RIGHT, SEG_LEFT]), concepts)
    assert len(coded) == 2
    assert all(c.scheme and c.code and c.meaning for c in coded)
    # The codes came from the dictionary, not from the card: the card named slugs.
    assert coded[0].meaning


def test_a_segment_without_structure_is_refused_by_name(concepts) -> None:
    with pytest.raises(WriterRefused, match="names no 'structure'"):
        _segment_concepts(_card([{"kind": "segmentation", "value": 1, "name": "x"}]), concepts)


def test_an_unknown_structure_is_refused_by_name(concepts) -> None:
    bad = {**SEG_RIGHT, "structure": "no_such_organ"}
    with pytest.raises(WriterRefused, match="no segment profile"):
        _segment_concepts(_card([bad]), concepts)


def test_the_label_map_stacks_masks_by_descriptor_order(concepts) -> None:
    pred = np.zeros((4, 8, 8), dtype=np.int16)
    pred[0:2, 0:4, 0:4] = 2   # left, second descriptor
    pred[2:4, 4:8, 4:8] = 1   # right, first descriptor
    result = SimpleNamespace(
        card=_card([SEG_RIGHT, SEG_LEFT]),
        model_output=SimpleNamespace(arrays={"pred": pred}, metrics={}),
        source_geometry=SimpleNamespace(array=np.zeros((4, 8, 8))),
    )
    segments = _segment_concepts(result.card, concepts)
    label_map = _label_map(result.card, result, segments)
    assert label_map is not None
    assert label_map.array.dtype == np.uint8
    assert label_map.array[0, 0, 0] == 2   # value 2 is the SECOND descriptor -> 2
    assert label_map.array[3, 7, 7] == 1   # value 1 is the FIRST descriptor -> 1
    assert len(label_map.segments) == 2


def test_a_mask_off_the_source_grid_is_refused(concepts) -> None:
    pred = np.zeros((3, 3, 3), dtype=np.int16)
    result = SimpleNamespace(
        card=_card([SEG_RIGHT]),
        model_output=SimpleNamespace(arrays={"pred": pred}, metrics={}),
        source_geometry=SimpleNamespace(hu_array=np.zeros((4, 8, 8), dtype=np.float32)),
    )
    segments = _segment_concepts(result.card, concepts)
    with pytest.raises(WriterRefused, match="SOURCE grid"):
        _label_map(result.card, result, segments)


def test_a_measurement_needs_a_dictionary_key(concepts) -> None:
    descriptor = {"kind": "measurement", "name": "vol", "from": "volume_ml", "unit": "ml"}
    result = SimpleNamespace(
        card=_card([descriptor]),
        model_output=SimpleNamespace(arrays={}, metrics={"volume_ml": 12.5}),
    )
    with pytest.raises(WriterRefused, match="concept_key"):
        _findings(result.card, result, concepts)


def test_requested_outputs_follow_the_descriptors(concepts) -> None:
    from medos.core.bundle import Finding, Measurement

    segs = _segment_concepts(_card([SEG_RIGHT]), concepts)
    with_meas = (
        Finding(kind="m", present=True, score=None, measurements=(
            Measurement(name=SimpleNamespace(scheme="SCT", code="1", meaning="m"),
                        value=1.0, unit="ml"),
        )),
    )
    assert _requested_outputs(segs, with_meas) == ("SEG", "SR")
    assert _requested_outputs((), with_meas) == ("SR",)
    assert _requested_outputs(segs, ()) == ("SEG",)
