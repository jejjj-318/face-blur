import numpy as np
import pytest
from pydantic import ValidationError

from app.masks import (
    AutoEdit,
    Box,
    ManualMask,
    MaskPlan,
    Settings,
    Tracker,
    blur_frame,
    iou,
)


def moving_mask(**updates):
    return ManualMask.model_validate(
        {
            "id": "manual",
            "start": 0,
            "end": 2,
            "keyframes": [
                {"time": 0, "box": {"x": 0.1, "y": 0.2, "w": 0.2, "h": 0.2}},
                {"time": 1, "box": {"x": 0.5, "y": 0.4, "w": 0.2, "h": 0.2}},
            ],
            **updates,
        }
    )


def test_interpolation_and_interval_are_frame_based():
    plan = MaskPlan([], Settings(manual=[moving_mask()]), 10)
    assert plan.boxes(5)[0] == pytest.approx([0.3, 0.3, 0.2, 0.2])
    assert plan.boxes(15)[0] == pytest.approx([0.5, 0.4, 0.2, 0.2])
    assert plan.boxes(20) == []


def test_tracker_holds_short_gap_then_expires():
    tracker = Tracker(hold_frames=2)
    tracker.update(0, [[0.1, 0.1, 0.2, 0.2]])
    tracker.update(1, [])
    tracker.update(2, [])
    tracker.update(3, [])
    tracker.update(4, [[0.1, 0.1, 0.2, 0.2]])
    tracks = tracker.result()
    assert len(tracks) == 2
    assert [s[0] for s in tracks[0]["samples"]] == [0, 1, 2]
    assert tracks[1]["samples"][0][0] == 4


def test_tracker_does_not_assign_one_box_to_two_tracks():
    tracker = Tracker(2)
    tracker.update(0, [[0.1, 0.1, 0.2, 0.2], [0.22, 0.1, 0.2, 0.2]])
    tracker.update(1, [[0.15, 0.1, 0.2, 0.2]])
    assert sum(t["last_seen"] == 1 for t in tracker.tracks) == 1


def test_auto_transform_and_disable():
    tracks = [{"id": 1, "samples": [[3, 0.1, 0.2, 0.2, 0.3], [4, 0.2, 0.2, 0.2, 0.3]]}]
    plan = MaskPlan(tracks, Settings(auto=[AutoEdit(id=1, dx=0.1, sy=2)]), 10)
    assert plan.boxes(3)[0] == pytest.approx([0.2, 0.2, 0.2, 0.6])
    assert plan.boxes(2) == []
    assert (
        MaskPlan(tracks, Settings(auto=[AutoEdit(id=1, enabled=False)]), 10).boxes(3)
        == []
    )


def test_blur_preserves_outside_and_reduces_local_variation():
    rng = np.random.default_rng(17)
    image = rng.integers(0, 256, (120, 160, 3), dtype=np.uint8)
    source = image.copy()
    blur_frame(image, [[0.25, 0.25, 0.5, 0.5]], 70)
    assert np.array_equal(image[:30], source[:30])
    assert image[35:85, 45:115].std() < source[35:85, 45:115].std() / 4
    assert not np.array_equal(image, source)


def test_strength_changes_result_and_clipping_is_safe():
    rng = np.random.default_rng(42)
    source = rng.integers(0, 256, (100, 100, 3), dtype=np.uint8)
    weak = blur_frame(source.copy(), [[0, 0, 1, 1]], 10)
    strong = blur_frame(source.copy(), [[0, 0, 1, 1]], 100)
    assert strong.std() < weak.std()
    result = blur_frame(source.copy(), [[-0.2, -0.2, 0.4, 0.4], [2, 2, 0.2, 0.2]], 70)
    assert np.array_equal(result[20:], source[20:])


def test_overlap_result_is_independent_of_input_order():
    source = np.random.default_rng(11).integers(0, 256, (80, 100, 3), dtype=np.uint8)
    boxes = [[0, 0, 0.6, 0.6], [0.3, 0.3, 0.6, 0.6]]
    assert np.array_equal(
        blur_frame(source.copy(), boxes, 70), blur_frame(source.copy(), boxes[::-1], 70)
    )


@pytest.mark.parametrize(
    "box",
    [
        {"x": 0.9, "y": 0, "w": 0.2, "h": 0.2},
        {"x": float("nan"), "y": 0, "w": 0.2, "h": 0.2},
        {"x": 0, "y": 0, "w": 0, "h": 0.2},
        {"x": 0, "y": -1, "w": 0.2, "h": 0.2},
    ],
)
def test_invalid_geometry_rejected(box):
    with pytest.raises(ValidationError):
        Box.model_validate(box)


@pytest.mark.parametrize(
    "updates",
    [
        {"start": 2, "end": 1},
        {"end": 0.5},
        {"keyframes": []},
        {"keyframes": [{"time": 0, "box": {"x": 0, "y": 0, "w": 0.2, "h": 0.2}}] * 2},
    ],
)
def test_invalid_keyframes_rejected(updates):
    with pytest.raises(ValidationError):
        moving_mask(**updates)


def test_video_references_and_intervals_are_validated():
    with pytest.raises(ValueError):
        Settings(auto=[AutoEdit(id=7)]).check_video([], 10)
    with pytest.raises(ValueError):
        Settings(manual=[moving_mask()]).check_video([], 1)
    with pytest.raises(ValueError):
        Settings(manual=[moving_mask(), moving_mask()]).check_video([], 3)


def test_iou_known_values():
    assert iou([0, 0, 1, 1], [0, 0, 1, 1]) == 1
    assert iou([0, 0, 1, 1], [1, 0, 1, 1]) == 0
    assert iou([0, 0, 2, 1], [1, 0, 2, 1]) == pytest.approx(1 / 3)
