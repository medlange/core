# SPDX-License-Identifier: Apache-2.0
"""A segmentation is placed on its image's grid, or the case is refused. Never approximated.

WHY THIS IS THE MOST DANGEROUS FUNCTION IN THE INGEST PATH
------------------------------------------------------------
`align_to_image` decides where a reader's contours sit in a patient. Get it wrong by one
slice and the labels move a slice through the body -- and nothing downstream notices,
because training simply converges a little worse and every metric is computed against the
same displaced labels. There is no assertion anywhere else in the pipeline that could
catch it. So the refusals are tested as carefully as the placements, and the round-trip
property (a shift applied and undone is the identity) is tested directly rather than
inferred from a worked example.

WHAT IT IS FOR, MEASURED. In `ct_spine_seg_100_10.02.2024`, 59 of 100 segmentations carry
one slice more than their image, 3 carry two, and 38 match exactly. Those 62 cases are not
corrupt: Slicer wrote the segmentation over a slightly larger extent and recorded the
origin it used. Dropping them would lose 62% of that corpus to a fixable bookkeeping
difference. Placing them WITHOUT checking the geometry would be worse than dropping them.

Spec: MOS-IMG-062 (identity rests on geometry), MOS-TRAIN-141.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.ingest.nnunet_dataset import Misaligned, align_to_image  # noqa: E402


def _header(sizes, origin=(0.0, 0.0, 0.0), spacing=(1.0, 1.0, 1.0), **extra):
    header = {
        "sizes": list(sizes),
        "space": "left-posterior-superior",
        "space origin": list(origin),
        "space directions": [
            [spacing[0], 0.0, 0.0],
            [0.0, spacing[1], 0.0],
            [0.0, 0.0, spacing[2]],
        ],
    }
    header.update(extra)
    return header


# --------------------------------------------------------------------------------------
# placement
# --------------------------------------------------------------------------------------


def test_identical_geometry_is_returned_unchanged() -> None:
    labels = np.arange(2 * 3 * 4, dtype=np.uint8).reshape(2, 3, 4)
    out = align_to_image(labels, (2, 3, 4), _header((2, 3, 4)), _header((2, 3, 4)))
    assert np.array_equal(out, labels)


def test_a_segmentation_longer_than_its_image_is_cropped_at_the_far_end() -> None:
    """The spine case with `extent offset 0 0 0` and one extra slice: same origin, so the
    two grids start together and the extra slice is past the end of the image."""
    labels = np.zeros((4, 4, 6), dtype=np.uint8)
    labels[..., :5] = 1
    labels[..., 5] = 9  # the slice that must be dropped
    out = align_to_image(labels, (4, 4, 5), _header((4, 4, 5)), _header((4, 4, 6)))
    assert out.shape == (4, 4, 5)
    assert np.all(out == 1)
    assert 9 not in np.unique(out)


def test_a_segmentation_starting_before_the_image_is_shifted_by_the_origin_delta() -> None:
    """The spine case with `extent offset 0 0 -1`: the segmentation's origin is one
    z-spacing BELOW the image's, so its slice 1 is the image's slice 0."""
    spacing = (1.0, 1.0, 0.8)
    labels = np.zeros((4, 4, 7), dtype=np.uint8)
    labels[..., 0] = 7  # before the image starts; must not appear
    labels[..., 1:6] = 3
    out = align_to_image(
        labels,
        (4, 4, 5),
        _header((4, 4, 5), origin=(0.0, 0.0, 0.0), spacing=spacing),
        _header((4, 4, 7), origin=(0.0, 0.0, -0.8), spacing=spacing,
                **{"Segmentation_ReferenceImageExtentOffset": "0 0 -1"}),
    )
    assert out.shape == (4, 4, 5)
    assert np.all(out == 3), np.unique(out)
    assert 7 not in np.unique(out), "content from before the image start leaked in"


def test_a_shift_and_its_inverse_return_the_original() -> None:
    """The round-trip property, asserted directly. If placement is a pure translation then
    shifting by +k and back by -k is the identity on the overlap, whatever k is."""
    rng = np.random.default_rng(0)
    labels = rng.integers(0, 4, size=(5, 5, 12), dtype=np.uint8)
    for k in (-3, -1, 0, 1, 3):
        forward = align_to_image(
            labels,
            (5, 5, 12),
            _header((5, 5, 12), origin=(0.0, 0.0, 0.0)),
            _header((5, 5, 12), origin=(0.0, 0.0, float(k))),
        )
        back = align_to_image(
            forward,
            (5, 5, 12),
            _header((5, 5, 12), origin=(0.0, 0.0, 0.0)),
            _header((5, 5, 12), origin=(0.0, 0.0, float(-k))),
        )
        overlap = slice(abs(k), 12 - abs(k)) if k else slice(None)
        assert np.array_equal(back[..., overlap], labels[..., overlap]), f"k={k}"


def test_the_shift_is_read_from_the_world_origin_and_not_from_the_sizes() -> None:
    """Two segmentations of the SAME size relative to the image, one offset and one not,
    must land in different places. A function that inferred the shift from the size
    difference would treat them identically -- and both spine readings above have a size
    difference of one, with different correct answers."""
    labels = np.zeros((3, 3, 5), dtype=np.uint8)
    labels[..., 0] = 1
    flush = align_to_image(labels, (3, 3, 5), _header((3, 3, 5)), _header((3, 3, 5)))
    shifted = align_to_image(
        labels, (3, 3, 5), _header((3, 3, 5)),
        _header((3, 3, 5), origin=(0.0, 0.0, 2.0)),
    )
    assert flush[0, 0, 0] == 1 and flush[0, 0, 2] == 0
    assert shifted[0, 0, 0] == 0 and shifted[0, 0, 2] == 1


# --------------------------------------------------------------------------------------
# refusals -- each of these would otherwise move labels through a patient
# --------------------------------------------------------------------------------------


def test_different_voxel_axes_are_refused_rather_than_translated() -> None:
    """The pneumonia case: the segmentation is on an isotropic 0.42mm grid while the image
    is 0.70 x 0.70 x 1.25. That is a resampling. Translating it would be nonsense, and the
    nonsense would look like a slightly worse Dice rather than like an error."""
    labels = np.zeros((4, 4, 4), dtype=np.uint8)
    with pytest.raises(Misaligned, match="resampling, not a shift"):
        align_to_image(
            labels, (4, 4, 4),
            _header((4, 4, 4), spacing=(0.703, 0.703, 1.25)),
            _header((4, 4, 4), spacing=(0.417, 0.417, 0.417)),
        )


def test_a_flipped_axis_is_refused() -> None:
    """A negative direction is a different orientation convention, not an offset. Placing
    it by translation would mirror the patient."""
    labels = np.zeros((4, 4, 4), dtype=np.uint8)
    seg = _header((4, 4, 4))
    seg["space directions"] = [[-1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, 1.0]]
    with pytest.raises(Misaligned, match="resampling, not a shift"):
        align_to_image(labels, (4, 4, 4), _header((4, 4, 4)), seg)


def test_a_sub_voxel_offset_is_refused() -> None:
    """Half a voxel cannot be expressed by copying a block, and rounding it would displace
    every label by up to half a slice with no record that it happened."""
    labels = np.zeros((4, 4, 4), dtype=np.uint8)
    with pytest.raises(Misaligned, match="not a whole number"):
        align_to_image(
            labels, (4, 4, 4), _header((4, 4, 4)),
            _header((4, 4, 4), origin=(0.0, 0.0, 0.5)),
        )


def test_slicers_own_offset_disagreeing_with_the_geometry_is_refused() -> None:
    """THE CORROBORATION CHECK. The origins decide; Slicer's bookkeeping field is read only
    to confirm. When the two disagree one of them is wrong about this case, and neither
    this function nor anything downstream can tell which -- so it refuses rather than
    silently preferring the one it happens to trust."""
    labels = np.zeros((4, 4, 6), dtype=np.uint8)
    with pytest.raises(Misaligned, match="disagree"):
        align_to_image(
            labels, (4, 4, 5), _header((4, 4, 5)),
            _header((4, 4, 6), origin=(0.0, 0.0, -1.0),
                    **{"Segmentation_ReferenceImageExtentOffset": "0 0 -3"}),
        )


def test_slicers_offset_agreeing_with_the_geometry_is_accepted() -> None:
    """The other half of the previous test: corroboration must not become an obstacle when
    the two sources agree, or 59 spine cases would still be dropped."""
    labels = np.ones((4, 4, 6), dtype=np.uint8)
    out = align_to_image(
        labels, (4, 4, 5), _header((4, 4, 5)),
        _header((4, 4, 6), origin=(0.0, 0.0, -1.0),
                **{"Segmentation_ReferenceImageExtentOffset": "0 0 -1"}),
    )
    assert out.shape == (4, 4, 5)
    assert np.all(out == 1)


def test_a_segmentation_that_does_not_overlap_at_all_is_refused() -> None:
    """An empty result is a valid array and a silent catastrophe: the case would stage,
    supervise its channel, and teach the model that the finding is absent everywhere."""
    labels = np.ones((4, 4, 4), dtype=np.uint8)
    with pytest.raises(Misaligned, match="does not overlap"):
        align_to_image(
            labels, (4, 4, 4), _header((4, 4, 4)),
            _header((4, 4, 4), origin=(0.0, 0.0, 99.0)),
        )


def test_label_values_are_never_interpolated() -> None:
    """Placement copies a block. If it ever grows an interpolation, a boundary voxel
    between label 1 and label 3 could become 2 -- a class nobody drew."""
    labels = np.zeros((2, 2, 6), dtype=np.uint8)
    labels[..., :3] = 1
    labels[..., 3:] = 3
    out = align_to_image(
        labels, (2, 2, 6), _header((2, 2, 6)),
        _header((2, 2, 6), origin=(0.0, 0.0, 1.0)),
    )
    assert set(np.unique(out).tolist()) <= {0, 1, 3}, np.unique(out)
