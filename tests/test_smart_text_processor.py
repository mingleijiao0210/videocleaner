from pathlib import Path

import cv2
import numpy as np

from app.coordinate_mapper import Rect
from app.smart_text_processor import (
    TextMaskConfig,
    detect_text_mask,
    repair_frame_region,
)


def synthetic_text_box() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    image = np.full((180, 420, 3), (70, 95, 120), np.uint8)
    cv2.rectangle(image, (40, 45), (380, 135), (248, 248, 248), -1)
    cv2.rectangle(image, (40, 45), (380, 135), (210, 210, 210), 2)
    text_truth = np.zeros(image.shape[:2], np.uint8)
    cv2.putText(
        image,
        "TEST 123",
        (80, 108),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.35,
        (120, 120, 120),
        3,
        cv2.LINE_AA,
    )
    cv2.putText(
        text_truth,
        "TEST 123",
        (80, 108),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.35,
        255,
        3,
        cv2.LINE_AA,
    )
    box_border = np.zeros(image.shape[:2], np.uint8)
    cv2.rectangle(box_border, (40, 45), (380, 135), 255, 2)
    return image, text_truth, box_border


def test_mask_covers_text_but_preserves_existing_box_border():
    image, text_truth, box_border = synthetic_text_box()
    roi = image[40:140, 35:385]
    mask = detect_text_mask(roi)
    truth_roi = text_truth[40:140, 35:385]
    border_roi = box_border[40:140, 35:385]
    text_coverage = cv2.countNonZero(cv2.bitwise_and(mask, truth_roi)) / max(
        cv2.countNonZero(truth_roi), 1
    )
    border_coverage = cv2.countNonZero(cv2.bitwise_and(mask, border_roi)) / max(
        cv2.countNonZero(border_roi), 1
    )
    assert text_coverage >= 0.90
    assert border_coverage <= 0.05


def test_repair_changes_only_detected_pixels_inside_selection():
    image, _text_truth, _box_border = synthetic_text_box()
    selection = Rect(35, 40, 350, 100)
    repaired, mask = repair_frame_region(
        image,
        selection,
        config=TextMaskConfig(dilation=2, inpaint_radius=4),
    )
    outside = np.ones(image.shape[:2], dtype=bool)
    outside[40:140, 35:385] = False
    assert np.array_equal(repaired[outside], image[outside])
    assert cv2.countNonZero(mask) > 0
    before_std = image[70:120, 70:350].std()
    after_std = repaired[70:120, 70:350].std()
    assert after_std < before_std * 0.45


def test_uniform_region_produces_empty_mask():
    roi = np.full((80, 300, 3), 245, np.uint8)
    mask = detect_text_mask(roi)
    assert cv2.countNonZero(mask) == 0
