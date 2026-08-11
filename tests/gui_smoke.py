"""启动开发版、导入合成视频、显示选框并保存界面截图后自动退出。"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.coordinate_mapper import Rect
from app.main_window import MainWindow


def main() -> int:
    app = QApplication([])
    app.setStyle("Fusion")
    window = MainWindow()
    window.load_video(ROOT / "test_media" / "横屏测试视频.mp4")
    window.preview.overlay.set_selection(Rect(430, 610, 420, 70))
    window.show()
    window.player.play()

    def capture() -> None:
        if window.player.position() < 500:
            raise RuntimeError(
                f"播放器位置未正常前进，当前位置 {window.player.position()} ms"
            )
        print(f"PLAYER_POSITION_MS={window.player.position()}")
        destination = ROOT / "output" / "开发版界面验证.png"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not window.grab().save(str(destination)):
            raise RuntimeError("界面截图保存失败")
        print(destination)
        window.close()
        app.quit()

    QTimer.singleShot(2500, capture)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
