"""生成无私人内容的双区域测试视频，并执行真实多选框导出。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coordinate_mapper import Rect
from app.ffmpeg_locator import require_ffmpeg
from app.ffmpeg_runner import ExportOptions, build_ffmpeg_args
from app.media_info import probe_media


SELECTIONS = (
    Rect(70, 60, 180, 54),
    Rect(390, 245, 180, 54),
)


def run(command: list[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)


def read_frame(path: Path) -> np.ndarray:
    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, 1000)
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok or frame is None:
        raise RuntimeError(f"无法读取测试帧：{path}")
    return frame


def main() -> int:
    tools = require_ffmpeg()
    source = ROOT / "test_media" / "多选框测试视频.mp4"
    output = ROOT / "output" / "integration" / "多选框_同时处理.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    source.unlink(missing_ok=True)
    output.unlink(missing_ok=True)

    run(
        [
            str(tools.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=25:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000:duration=3",
            "-vf",
            (
                "drawbox=x=70:y=60:w=180:h=54:color=white@0.95:t=fill,"
                "drawbox=x=390:y=245:w=180:h=54:color=yellow@0.95:t=fill"
            ),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ]
    )
    source_info = probe_media(tools.ffprobe, source)
    options = ExportOptions(
        input_path=source,
        output_path=output,
        selection=SELECTIONS[0],
        selections=SELECTIONS,
        video_width=source_info.width,
        video_height=source_info.height,
        duration=source_info.duration,
        audio_codec=source_info.audio_codec,
        effect_mode="delogo",
        preset="veryfast",
    )
    args = build_ffmpeg_args(options)
    filter_value = args[args.index("-vf") + 1]
    assert filter_value.count("delogo=") == 2
    run([str(tools.ffmpeg), *args])

    output_info = probe_media(tools.ffprobe, output)
    assert (output_info.width, output_info.height) == (640, 360)
    assert abs(output_info.duration - source_info.duration) <= 0.15
    assert output_info.video_codec == "h264"
    assert output_info.has_audio

    before = read_frame(source)
    after = read_frame(output)
    region_differences = []
    for selection in SELECTIONS:
        x, y = round(selection.x), round(selection.y)
        width, height = round(selection.width), round(selection.height)
        difference = np.mean(
            np.abs(
                before[y : y + height, x : x + width].astype(np.int16)
                - after[y : y + height, x : x + width].astype(np.int16)
            )
        )
        region_differences.append(round(float(difference), 2))
    assert all(value > 5 for value in region_differences)

    print(
        json.dumps(
            {
                "source": str(source),
                "output": str(output),
                "selection_count": len(SELECTIONS),
                "filter_count": filter_value.count("delogo="),
                "region_differences": region_differences,
                "resolution": f"{output_info.width}x{output_info.height}",
                "duration": output_info.duration,
                "video_codec": output_info.video_codec,
                "audio_codec": output_info.audio_codec,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
