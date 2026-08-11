import math

import pytest

from app.coordinate_mapper import (
    Rect,
    adjust_rect_for_yuv420p,
    calculate_video_content_rect,
    preview_to_video_point,
    preview_to_video_rect,
    video_to_preview_rect,
)


def test_horizontal_video_has_top_and_bottom_bars():
    content = calculate_video_content_rect(1000, 1000, 1280, 720)
    assert content.x == pytest.approx(0)
    assert content.y == pytest.approx(218.75)
    assert content.width == pytest.approx(1000)
    assert content.height == pytest.approx(562.5)


def test_vertical_video_has_left_and_right_bars():
    content = calculate_video_content_rect(1000, 1000, 720, 1280)
    assert content.x == pytest.approx(218.75)
    assert content.y == pytest.approx(0)
    assert content.width == pytest.approx(562.5)
    assert content.height == pytest.approx(1000)


def test_black_bar_point_clamps_to_video_edge():
    content = calculate_video_content_rect(1000, 1000, 1280, 720)
    assert preview_to_video_point(500, 0, content, 1280, 720) == pytest.approx(
        (640, 0)
    )


@pytest.mark.parametrize(
    ("container", "video", "original"),
    [
        ((1000, 700), (1280, 720), Rect(201, 99, 411, 107)),
        ((800, 900), (720, 1280), Rect(25, 500, 320, 200)),
        ((1920, 1080), (3840, 2160), Rect(1000, 700, 800, 220)),
    ],
)
def test_round_trip_coordinates(container, video, original):
    content = calculate_video_content_rect(*container, *video)
    preview = video_to_preview_rect(original, content, *video)
    restored = preview_to_video_rect(preview, content, *video)
    assert restored.x == pytest.approx(original.x)
    assert restored.y == pytest.approx(original.y)
    assert restored.width == pytest.approx(original.width)
    assert restored.height == pytest.approx(original.height)


def test_window_resize_keeps_video_coordinates():
    original = Rect(100, 50, 350, 80)
    first = calculate_video_content_rect(800, 600, 1280, 720)
    second = calculate_video_content_rect(1400, 700, 1280, 720)
    first_preview = video_to_preview_rect(original, first, 1280, 720)
    second_preview = video_to_preview_rect(original, second, 1280, 720)
    assert first_preview != second_preview
    restored = preview_to_video_rect(second_preview, second, 1280, 720)
    assert restored.x == pytest.approx(original.x)
    assert restored.y == pytest.approx(original.y)
    assert restored.width == pytest.approx(original.width)
    assert restored.height == pytest.approx(original.height)


def test_odd_rect_is_safely_adjusted_and_bounded():
    x, y, width, height = adjust_rect_for_yuv420p(
        Rect(1271, 711, 9, 9), 1280, 720
    )
    assert x >= 2 and y >= 2
    assert width >= 4 and height >= 4
    assert x + width <= 1278
    assert y + height <= 718


def test_full_width_selection_is_inset_for_delogo_interpolation():
    assert adjust_rect_for_yuv420p(
        Rect(0, 26, 544, 198), 544, 960
    ) == (2, 26, 540, 198)


def test_top_and_bottom_edge_selection_is_inset():
    x, y, width, height = adjust_rect_for_yuv420p(
        Rect(20, 0, 100, 960), 544, 960
    )
    assert (x, y, width, height) == (20, 2, 100, 956)


def test_invalid_dimensions_return_empty_content():
    assert calculate_video_content_rect(0, 600, 1280, 720) == Rect(0, 0, 0, 0)
