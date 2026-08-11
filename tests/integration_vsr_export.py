"""实际执行本地 STTN/DirectML 精准去字并验证最终视频属性。"""

from __future__ import annotations

import json
import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coordinate_mapper import Rect
from app.ffmpeg_locator import require_ffmpeg
from app.ffmpeg_runner import ExportOptions
from app.media_info import probe_media
from app.vsr_runner import VSRRunner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("ai_strong", "ai_fast", "ai_precise", "ai_full"),
        default="ai_precise",
    )
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--coords",
        nargs=4,
        type=float,
        metavar=("X", "Y", "W", "H"),
    )
    args = parser.parse_args()
    app = QCoreApplication([])
    tools = require_ffmpeg()
    source = (args.input or ROOT / "test_media" / "vsr_short_test.mp4").resolve()
    destination = (
        args.output or ROOT / "output" / f"vsr_directml_{args.mode}.mp4"
    ).resolve()
    destination.unlink(missing_ok=True)
    source_info = probe_media(tools.ffprobe, source)
    selection = (
        Rect(*args.coords)
        if args.coords is not None
        else Rect(65, 240, 510, 95)
    )
    options = ExportOptions(
        input_path=source,
        output_path=destination,
        selection=selection,
        video_width=source_info.width,
        video_height=source_info.height,
        duration=source_info.duration,
        audio_codec=source_info.audio_codec,
        effect_mode=args.mode,
        preset="medium",
        crf=18,
    )
    runner = VSRRunner(tools.ffmpeg)
    errors: list[str] = []
    runner.statusChanged.connect(lambda text: print(f"STATUS {text}", flush=True))
    runner.progressChanged.connect(
        lambda percent, processed, total: print(
            f"PROGRESS {percent:.1f}% {processed:.3f}/{total:.3f}",
            flush=True,
        )
    )
    runner.errorOccurred.connect(errors.append)
    runner.start_with(options)
    if not runner.wait(1_200_000):
        runner.cancel()
        runner.wait(10_000)
        raise TimeoutError("本地 AI 集成测试超过 20 分钟")
    if errors:
        raise RuntimeError(errors[-1])
    if not destination.is_file():
        raise RuntimeError("本地 AI 没有生成输出视频")
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
                "resolution": f"{output_info.width}x{output_info.height}",
                "duration": output_info.duration,
                "video_codec": output_info.video_codec,
                "audio_codec": output_info.audio_codec,
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
