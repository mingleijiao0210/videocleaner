"""对合成横屏和竖屏视频执行真实 FFmpeg 导出并验证媒体属性。"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coordinate_mapper import Rect, adjust_rect_for_yuv420p
from app.ffmpeg_locator import require_ffmpeg
from app.ffmpeg_runner import (
    ExportOptions,
    build_ffmpeg_args,
    create_rounded_cover_image,
    resolve_effect_mode,
)
from app.media_info import probe_media


def export_and_verify(
    source: Path,
    destination: Path,
    selection: Rect,
    time_range: tuple[float, float] | None,
) -> dict[str, object]:
    tools = require_ffmpeg()
    source_info = probe_media(tools.ffprobe, source)
    start, end = time_range or (None, None)
    options = ExportOptions(
        input_path=source,
        output_path=destination,
        selection=selection,
        video_width=source_info.width,
        video_height=source_info.height,
        duration=source_info.duration,
        audio_codec=source_info.audio_codec,
        range_start=start,
        range_end=end,
        preset="medium",
        crf=18,
    )
    temporary_cover = None
    if resolve_effect_mode(options) == "solid_cover":
        _x, _y, cover_width, cover_height = adjust_rect_for_yuv420p(
            selection, source_info.width, source_info.height
        )
        temporary_cover = destination.with_name(
            f".{destination.stem}_cover.png"
        )
        create_rounded_cover_image(temporary_cover, cover_width, cover_height)
        options = replace(options, cover_image_path=temporary_cover)
    try:
        result = subprocess.run(
            [str(tools.ffmpeg), *build_ffmpeg_args(options)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=300,
        )
    finally:
        if temporary_cover is not None:
            temporary_cover.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(result.stderr)
    output_info = probe_media(tools.ffprobe, destination)
    assert (output_info.width, output_info.height) == (
        source_info.width,
        source_info.height,
    )
    assert abs(output_info.duration - source_info.duration) <= 0.15
    assert output_info.video_codec == "h264"
    assert output_info.has_audio == source_info.has_audio
    if source_info.audio_codec == "opus":
        assert output_info.audio_codec == "aac"
    return {
        "source": source.name,
        "output": destination.name,
        "resolution": f"{output_info.width}x{output_info.height}",
        "duration": output_info.duration,
        "video_codec": output_info.video_codec,
        "audio_codec": output_info.audio_codec,
        "range": time_range or "entire",
    }


def main() -> int:
    output_dir = ROOT / "output" / "integration"
    output_dir.mkdir(parents=True, exist_ok=True)
    reports = [
        export_and_verify(
            ROOT / "test_media" / "横屏测试视频.mp4",
            output_dir / "横屏_完整处理.mp4",
            Rect(430, 610, 420, 70),
            None,
        ),
        export_and_verify(
            ROOT / "test_media" / "竖屏测试视频.mp4",
            output_dir / "竖屏_时间段处理.mp4",
            Rect(100, 1120, 520, 90),
            (2.0, 6.0),
        ),
        export_and_verify(
            ROOT / "test_media" / "中文 空格(无音频).mp4",
            output_dir / "中文 空格(无音频)_已处理.mp4",
            Rect(180, 290, 280, 45),
            None,
        ),
        export_and_verify(
            ROOT / "test_media" / "中文 空格(不兼容音频).mkv",
            output_dir / "中文 空格(自动转AAC)_已处理.mp4",
            Rect(180, 290, 280, 45),
            None,
        ),
    ]
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
