from pathlib import Path

import cv2
import numpy as np

from app.coordinate_mapper import Rect
from app.ffmpeg_runner import ExportOptions
from app.vsr_locator import VSRPaths
from app.vsr_runner import (
    VSRRunner,
    build_ai_finalize_command,
    build_vsr_worker_command,
)

from tools.vsr.bridge.videocleaner_vsr_worker import (
    _balanced_inference_size,
    _balanced_propainter_crop_rect,
    _create_cropped_box_mask,
    _feather_inpaint_boundaries,
    _mask_deviation_for_mode,
    _propagate_boxes_backward,
    _propagate_boxes_temporally,
    _propainter_crop_rect,
    _refine_overlay_text_mask,
    _safe_continuous_ranges,
    _safe_merge_intervals,
    _strong_crop_rect,
)


def _options(tmp_path: Path, mode: str = "ai_precise") -> ExportOptions:
    source = tmp_path / "中文 输入 (1).mp4"
    source.write_bytes(b"video")
    return ExportOptions(
        input_path=source,
        output_path=tmp_path / "输出 文件.mp4",
        selection=Rect(11, 21, 101, 41),
        video_width=640,
        video_height=360,
        duration=10.0,
        audio_codec="aac",
        effect_mode=mode,
    )


def test_worker_command_keeps_each_path_as_one_argument(tmp_path):
    engine = tmp_path / "引擎 目录"
    paths = VSRPaths(
        engine_root=engine,
        python=engine / "python.exe",
        backend_main=engine / "backend" / "main.py",
        bridge_worker=tmp_path / "桥接 脚本.py",
    )
    options = _options(tmp_path)
    command = build_vsr_worker_command(paths, options, tmp_path / "中间 文件.mp4")
    assert command[0] == str(paths.python)
    assert command[2] == str(paths.bridge_worker)
    assert command[command.index("--input") + 1] == str(options.input_path)
    assert command[command.index("--mode") + 1] == "precise"
    coords = command[command.index("--coords") + 1 : command.index("--mode")]
    assert coords == ["20", "62", "10", "112"]


def test_worker_command_can_forward_native_ffmpeg(tmp_path):
    engine = tmp_path / "engine"
    paths = VSRPaths(
        engine,
        engine / "python.exe",
        engine / "backend" / "main.py",
        tmp_path / "worker.py",
    )
    ffmpeg = tmp_path / "原生 ffmpeg"
    command = build_vsr_worker_command(
        paths,
        _options(tmp_path, "ai_propainter"),
        tmp_path / "temp.mp4",
        ffmpeg=ffmpeg,
    )
    assert command[command.index("--ffmpeg") + 1] == str(ffmpeg)


def test_worker_command_forwards_processing_time_range(tmp_path):
    options = _options(tmp_path, "ai_strong")
    options = ExportOptions(
        **{
            **options.__dict__,
            "range_start": 1.25,
            "range_end": 4.5,
        }
    )
    engine = tmp_path / "engine"
    paths = VSRPaths(
        engine,
        engine / "python.exe",
        engine / "backend" / "main.py",
        tmp_path / "worker.py",
    )

    command = build_vsr_worker_command(
        paths,
        options,
        tmp_path / "temp.mp4",
    )

    assert command[command.index("--range-start") + 1] == "1.250000"
    assert command[command.index("--range-end") + 1] == "4.500000"


def test_full_mode_is_forwarded_to_worker(tmp_path):
    options = _options(tmp_path, "ai_full")
    engine = tmp_path / "engine"
    paths = VSRPaths(
        engine,
        engine / "python.exe",
        engine / "backend" / "main.py",
        tmp_path / "worker.py",
    )
    command = build_vsr_worker_command(paths, options, tmp_path / "temp.mp4")
    assert command[-1] == "full"


def test_fast_mode_is_forwarded_to_worker(tmp_path):
    options = _options(tmp_path, "ai_fast")
    engine = tmp_path / "engine"
    paths = VSRPaths(
        engine,
        engine / "python.exe",
        engine / "backend" / "main.py",
        tmp_path / "worker.py",
    )
    command = build_vsr_worker_command(paths, options, tmp_path / "temp.mp4")
    assert command[-1] == "fast"


def test_strong_mode_is_forwarded_to_worker(tmp_path):
    options = _options(tmp_path, "ai_strong")
    engine = tmp_path / "engine"
    paths = VSRPaths(
        engine,
        engine / "python.exe",
        engine / "backend" / "main.py",
        tmp_path / "worker.py",
    )
    command = build_vsr_worker_command(paths, options, tmp_path / "temp.mp4")
    assert command[command.index("--mode") + 1] == "strong"


def test_propainter_mode_is_forwarded_to_worker(tmp_path):
    options = _options(tmp_path, "ai_propainter")
    engine = tmp_path / "engine"
    paths = VSRPaths(
        engine,
        engine / "python.exe",
        engine / "backend" / "main.py",
        tmp_path / "worker.py",
    )
    command = build_vsr_worker_command(paths, options, tmp_path / "temp.mp4")
    assert command[-1] == "propainter"


def test_balanced_propainter_mode_is_forwarded_to_worker(tmp_path):
    options = _options(tmp_path, "ai_propainter_fast")
    engine = tmp_path / "engine"
    paths = VSRPaths(
        engine,
        engine / "python.exe",
        engine / "backend" / "main.py",
        tmp_path / "worker.py",
    )
    command = build_vsr_worker_command(paths, options, tmp_path / "temp.mp4")
    assert command[-1] == "propainter_fast"


def test_fast_mode_propagates_future_boxes_to_subtitle_lead_frames():
    first_box = (100, 300, 900, 950)
    fuller_box = (80, 560, 895, 960)
    detected = {
        13: [first_box],
        19: [fuller_box],
    }

    padded = _propagate_boxes_backward(detected, lead_frames=12)

    assert padded[1] == [first_box]
    assert first_box in padded[7]
    assert fuller_box in padded[7]
    assert padded[19] == [fuller_box]


def test_fast_mode_propagates_boxes_through_short_fade_out():
    box = (80, 560, 895, 960)

    padded = _propagate_boxes_temporally(
        {19: [box]},
        lead_frames=12,
        trail_frames=12,
    )

    assert padded[7] == [box]
    assert padded[19] == [box]
    assert padded[31] == [box]


def test_sttn_boundary_feather_keeps_source_outside_and_repair_in_core():
    source = np.full((21, 21, 3), 200, dtype=np.uint8)
    repaired = np.full((21, 21, 3), 80, dtype=np.uint8)
    mask = np.zeros((21, 21), dtype=np.uint8)
    mask[5:16, 5:16] = 255

    feathered = _feather_inpaint_boundaries(
        [source],
        [repaired],
        mask,
        feather_pixels=4,
    )

    assert feathered[0][0, 0].tolist() == [200, 200, 200]
    assert feathered[0][10, 10].tolist() == [80, 80, 80]
    edge_value = int(feathered[0][5, 10, 0])
    assert 80 < edge_value < 200


def test_overlay_mask_keeps_character_strokes_not_the_ocr_rectangle():
    frames = []
    for index in range(8):
        frame = np.full((80, 180, 3), (35, 125 + index, 45), dtype=np.uint8)
        cv2.putText(
            frame,
            "ABC",
            (28, 55),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.35,
            (255, 255, 255),
            3,
            cv2.LINE_AA,
        )
        frames.append(frame)
    rectangle = np.zeros((80, 180), dtype=np.uint8)
    rectangle[8:72, 12:168] = 255

    refined = _refine_overlay_text_mask(frames, rectangle)

    assert np.count_nonzero(refined) > 400
    assert np.count_nonzero(refined) < np.count_nonzero(rectangle) * 0.40
    assert not np.any(refined[:8])
    assert not np.any(refined[:, :12])


def test_overlay_mask_covers_multicolor_latin_and_cjk_strokes():
    frames = []
    truth = np.zeros((100, 300), dtype=np.uint8)
    for _index in range(8):
        frame = np.full((100, 300, 3), (80, 100, 110), dtype=np.uint8)
        cv2.putText(
            frame,
            "彩色 ABC",
            (20, 65),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.3,
            (0, 255, 0),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            frame,
            "彩色 ABC",
            (145, 65),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.3,
            (255, 0, 255),
            3,
            cv2.LINE_AA,
        )
        frames.append(frame)
    cv2.putText(
        truth,
        "彩色 ABC",
        (20, 65),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.3,
        255,
        3,
        cv2.LINE_AA,
    )
    cv2.putText(
        truth,
        "彩色 ABC",
        (145, 65),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.3,
        255,
        3,
        cv2.LINE_AA,
    )
    rectangle = np.zeros((100, 300), dtype=np.uint8)
    rectangle[10:85, 5:295] = 255
    refined = _refine_overlay_text_mask(frames, rectangle)
    coverage = np.count_nonzero(cv2.bitwise_and(refined, truth)) / max(
        np.count_nonzero(truth), 1
    )
    assert coverage >= 0.90
    assert np.count_nonzero(refined) < np.count_nonzero(rectangle) * 0.55


def test_ai_text_modes_use_controlled_outline_margin():
    assert _mask_deviation_for_mode("strong") == 12
    assert _mask_deviation_for_mode("fast") == 12
    assert _mask_deviation_for_mode("precise") == 8
    assert _mask_deviation_for_mode("full") == 0


def test_no_detected_text_returns_empty_intervals_without_calling_upstream():
    def should_not_run(_value):
        raise AssertionError("empty input must bypass upstream implementation")

    assert _safe_continuous_ranges(should_not_run, {}) == []
    assert _safe_merge_intervals(should_not_run, []) == []


def test_strong_crop_is_stable_four_to_one_and_inside_frame():
    crop = _strong_crop_rect(720, 1280, (894, 978, 2, 718))
    assert crop == (0, 846, 720, 1026)
    x1, y1, x2, y2 = _strong_crop_rect(720, 1280, (10, 70, 200, 500))
    assert 0 <= x1 < x2 <= 720
    assert 0 <= y1 < y2 <= 1280
    assert (x2 - x1) / (y2 - y1) == 4.0


def test_strong_direct_crop_mask_matches_previous_full_frame_mask_exactly():
    frame_height, frame_width = 1280, 720
    crop = (0, 846, 720, 1026)
    boxes = [(28, 310, 880, 930), (340, 690, 900, 958)]
    deviation = 12
    full_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
    for xmin, xmax, ymin, ymax in boxes:
        cv2.rectangle(
            full_mask,
            (max(0, xmin - deviation), max(0, ymin - deviation)),
            (xmax + deviation, ymax + deviation),
            255,
            thickness=-1,
        )
    expected = full_mask[crop[1] : crop[3], crop[0] : crop[2]]

    actual = _create_cropped_box_mask(crop, boxes, deviation)

    assert np.array_equal(actual, expected)


def test_propainter_crop_preserves_selection_context_and_mps_alignment():
    x1, y1, x2, y2 = _propainter_crop_rect(
        1920,
        1080,
        (850, 980, 250, 1670),
    )
    assert x1 <= 250 < 1670 <= x2
    assert y1 <= 850 < 980 <= y2
    assert (x2 - x1) % 8 == 0
    assert (y2 - y1) % 8 == 0
    assert (x2 - x1) <= 1920
    assert (y2 - y1) <= 720


def test_propainter_mask_margin_covers_text_outline():
    assert _mask_deviation_for_mode("propainter") == 12
    assert _mask_deviation_for_mode("propainter_fast") == 12


def test_balanced_propainter_crop_reduces_context_without_cutting_selection():
    selection = (850, 980, 250, 1670)
    full = _propainter_crop_rect(1920, 1080, selection)
    balanced = _balanced_propainter_crop_rect(1920, 1080, selection)
    x1, y1, x2, y2 = balanced

    assert x1 <= 250 < 1670 <= x2
    assert y1 <= 850 < 980 <= y2
    assert (x2 - x1) % 8 == 0
    assert (y2 - y1) % 8 == 0
    assert (x2 - x1) * (y2 - y1) < (full[2] - full[0]) * (full[3] - full[1])


def test_balanced_propainter_inference_canvas_preserves_aspect_and_caps_pixels():
    width, height = _balanced_inference_size(1920, 720)

    assert width == 960
    assert height == 360
    assert width % 8 == 0
    assert height % 8 == 0
    assert width * height < 1920 * 720
    assert _balanced_inference_size(640, 240) == (640, 240)


def test_strong_finalize_range_uses_single_video_encode_and_original_audio(tmp_path):
    options = _options(tmp_path, "ai_strong")
    options = ExportOptions(
        **{
            **options.__dict__,
            "range_start": 1.25,
            "range_end": 4.5,
        }
    )
    command = build_ai_finalize_command(
        Path("ffmpeg.exe"),
        options,
        tmp_path / "repaired.mp4",
        copy_audio=True,
    )
    assert "-filter_complex" not in command
    assert command[command.index("-map") + 1] == "1:v:0"
    assert "0:a?" in command
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "copy"
    assert "-shortest" not in command


def test_non_strong_finalize_range_keeps_legacy_blend(tmp_path):
    options = _options(tmp_path, "ai_precise")
    options = ExportOptions(
        **{
            **options.__dict__,
            "range_start": 1.25,
            "range_end": 4.5,
        }
    )

    command = build_ai_finalize_command(
        Path("ffmpeg.exe"),
        options,
        tmp_path / "repaired.mp4",
        copy_audio=True,
    )

    filter_text = command[command.index("-filter_complex") + 1]
    assert "between(T,1.250,4.500)" in filter_text
    assert command[command.index("-map") + 1] == "[vout]"


def test_ai_runner_processes_multiple_selections_in_sequence(
    tmp_path, monkeypatch
):
    options = _options(tmp_path, "ai_fast")
    first = Rect(11, 21, 101, 41)
    second = Rect(301, 201, 121, 51)
    options = ExportOptions(
        **{
            **options.__dict__,
            "selection": first,
            "selections": (first, second),
        }
    )
    engine = tmp_path / "engine"
    paths = VSRPaths(
        engine,
        engine / "python.exe",
        engine / "backend" / "main.py",
        tmp_path / "worker.py",
    )
    runner = VSRRunner(tmp_path / "ffmpeg.exe")
    runner._options = options
    runner._paths = paths
    stages = []

    def fake_worker(
        _paths,
        stage_options,
        intermediate,
        _error_lines,
        selection_index=0,
        selection_count=1,
    ):
        stages.append(
            (
                stage_options.input_path,
                stage_options.selection,
                selection_index,
                selection_count,
            )
        )
        intermediate.write_bytes(b"repaired")

    def fake_finalize(
        final_options,
        intermediate,
        copy_audio,
        error_lines,
    ):
        assert intermediate.name == "repaired_engine_2.mp4"
        assert copy_audio
        final_options.output_path.write_bytes(b"final")
        return 0

    monkeypatch.setattr(runner, "_run_worker", fake_worker)
    monkeypatch.setattr(runner, "_run_finalize", fake_finalize)
    runner.run()

    assert len(stages) == 2
    assert stages[0] == (options.input_path, first, 0, 2)
    assert stages[1][0].name == "repaired_engine_1.mp4"
    assert stages[1][1:] == (second, 1, 2)
    assert options.output_path.is_file()
