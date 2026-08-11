"""验证 AI 指定时间段不会裁掉前后画面，并保留原音频。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coordinate_mapper import Rect
from app.ffmpeg_locator import require_ffmpeg
from app.ffmpeg_runner import ExportOptions
from app.media_info import probe_media
from app.vsr_runner import build_ai_finalize_command


def _frame(path: Path, seconds: float) -> np.ndarray:
    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, seconds * 1000)
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok or frame is None:
        raise RuntimeError(f"无法读取测试帧：{path}")
    return frame


def _mad(first: np.ndarray, second: np.ndarray) -> float:
    return float(np.abs(first.astype(np.float32) - second.astype(np.float32)).mean())


def main() -> int:
    tools = require_ffmpeg()
    source = ROOT / "test_media" / "vsr_short_test.mp4"
    repaired = ROOT / "output" / "vsr_directml_integration.mp4"
    destination = ROOT / "output" / "vsr_range_integration.mp4"
    if not repaired.is_file():
        raise FileNotFoundError("请先运行 integration_vsr_export.py")
    destination.unlink(missing_ok=True)
    info = probe_media(tools.ffprobe, source)
    options = ExportOptions(
        input_path=source,
        output_path=destination,
        selection=Rect(65, 240, 510, 95),
        video_width=info.width,
        video_height=info.height,
        duration=info.duration,
        audio_codec=info.audio_codec,
        range_start=0.5,
        range_end=1.5,
        effect_mode="ai_precise",
    )
    command = build_ai_finalize_command(
        tools.ffmpeg,
        options,
        repaired,
        copy_audio=True,
    )
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:])
    output_info = probe_media(tools.ffprobe, destination)
    assert abs(output_info.duration - info.duration) <= 0.15
    assert (output_info.width, output_info.height) == (info.width, info.height)
    assert output_info.has_audio

    outside_original = _frame(source, 0.2)
    outside_ranged = _frame(destination, 0.2)
    inside_original = _frame(source, 1.0)[240:336, 64:576]
    inside_repaired = _frame(repaired, 1.0)[240:336, 64:576]
    inside_ranged = _frame(destination, 1.0)[240:336, 64:576]
    outside_difference = _mad(outside_original, outside_ranged)
    inside_matches_repair = _mad(inside_repaired, inside_ranged)
    inside_differs_from_original = _mad(inside_original, inside_ranged)
    assert outside_difference < 5.0
    assert inside_matches_repair < 5.0
    assert inside_differs_from_original > 5.0
    print(
        {
            "outside_difference": round(outside_difference, 3),
            "inside_matches_repair": round(inside_matches_repair, 3),
            "inside_differs_from_original": round(inside_differs_from_original, 3),
            "duration": output_info.duration,
            "audio": output_info.audio_codec,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
