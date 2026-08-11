"""真实启动本地 AI 后取消，验证子进程与未完成文件被清理。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coordinate_mapper import Rect
from app.ffmpeg_locator import require_ffmpeg
from app.ffmpeg_runner import ExportOptions
from app.media_info import probe_media
from app.vsr_runner import VSRRunner


def main() -> int:
    app = QCoreApplication([])
    tools = require_ffmpeg()
    source = ROOT / "test_media" / "vsr_short_test.mp4"
    destination = ROOT / "output" / "vsr_should_be_cancelled.mp4"
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
        effect_mode="ai_precise",
    )
    runner = VSRRunner(tools.ffmpeg)
    outcomes: list[str] = []
    runner.cancelled.connect(lambda: (outcomes.append("cancelled"), app.quit()))
    runner.completed.connect(lambda _path: (outcomes.append("completed"), app.quit()))
    runner.errorOccurred.connect(lambda text: (outcomes.append(text), app.quit()))
    runner.start_with(options)
    QTimer.singleShot(2500, runner.cancel)
    QTimer.singleShot(30_000, app.quit)
    app.exec()
    runner.wait(10_000)
    assert outcomes == ["cancelled"], outcomes
    assert not destination.exists()
    assert not list(destination.parent.glob(".videocleaner_ai_*"))
    print("AI 取消测试通过：输出、临时目录和工作线程均已清理")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
