"""真实执行智能精确去字，并验证输出媒体属性和音频。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coordinate_mapper import Rect
from app.ffmpeg_locator import require_ffmpeg
from app.ffmpeg_runner import ExportOptions
from app.media_info import probe_media
from app.smart_text_processor import SmartTextRunner


def main() -> int:
    app = QCoreApplication([])
    tools = require_ffmpeg()
    source = ROOT / "test_media" / "smart_text_test.mp4"
    destination = ROOT / "output" / "smart_text_integration.mp4"
    if not source.is_file():
        raise FileNotFoundError("缺少 test_media\\smart_text_test.mp4")
    destination.unlink(missing_ok=True)
    source_info = probe_media(tools.ffprobe, source)
    selections = (
        Rect(65, 240, 510, 95),
        Rect(30, 30, 160, 55),
    )
    options = ExportOptions(
        input_path=source,
        output_path=destination,
        selection=selections[0],
        selections=selections,
        video_width=source_info.width,
        video_height=source_info.height,
        duration=source_info.duration,
        audio_codec=source_info.audio_codec,
        effect_mode="smart_text",
        preset="medium",
        crf=18,
    )
    runner = SmartTextRunner(tools.ffmpeg)
    runner.start_with(options)
    if not runner.wait(180_000):
        runner.cancel()
        runner.wait(5000)
        raise TimeoutError("智能精确去字集成测试超时")
    if not destination.is_file():
        raise RuntimeError("智能精确去字没有生成输出文件")
    output_info = probe_media(tools.ffprobe, destination)
    assert (output_info.width, output_info.height) == (
        source_info.width,
        source_info.height,
    )
    assert abs(output_info.duration - source_info.duration) <= 0.15
    assert output_info.video_codec == "h264"
    assert output_info.has_audio
    print(
        json.dumps(
            {
                "output": str(destination),
                "selection_count": len(selections),
                "resolution": f"{output_info.width}x{output_info.height}",
                "duration": output_info.duration,
                "video_codec": output_info.video_codec,
                "audio_codec": output_info.audio_codec,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
