"""应用入口。"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .logger import LOGGER
from .main_window import MainWindow
from .coordinate_mapper import Rect
from .settings import APP_NAME, APP_VERSION


def _exception_hook(exc_type, exc_value, exc_traceback) -> None:
    LOGGER.critical(
        "未捕获异常:\n%s",
        "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)),
    )
    QMessageBox.critical(
        None,
        "程序发生错误",
        "程序遇到未预期的错误，详细信息已写入 logs\\videocleaner.log。",
    )


def main() -> int:
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setApplicationVersion(APP_VERSION)
    QCoreApplication.setOrganizationName("LocalVideoCleaner")
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    sys.excepthook = _exception_hook
    try:
        window = MainWindow()
    except Exception as exc:
        LOGGER.exception("程序启动失败")
        QMessageBox.critical(None, "无法启动", str(exc))
        return 1
    window.show()
    if "--package-smoke" in sys.argv:
        try:
            index = sys.argv.index("--package-smoke")
            source = Path(sys.argv[index + 1]).resolve()
            output = Path(sys.argv[index + 2]).resolve()
            screenshot = Path(sys.argv[index + 3]).resolve()
        except (ValueError, IndexError):
            LOGGER.error("--package-smoke 参数不足")
            return 2
        window.automated_test = True
        window.load_video(source)
        if window.media_info is None:
            return 2
        width, height = window.media_info.width, window.media_info.height
        selection_values = sys.argv[index + 4 : index + 8]
        try:
            coordinates = (
                tuple(float(value) for value in selection_values)
                if len(selection_values) == 4
                and not any(value.startswith("--") for value in selection_values)
                else None
            )
        except ValueError:
            coordinates = None
        if coordinates is None:
            selection = Rect(
                width * 0.33, height * 0.84, width * 0.34, height * 0.10
            )
        else:
            selection = Rect(*coordinates)
        window.preview.overlay.set_selection(selection)
        if "--package-second-selection" in sys.argv:
            try:
                second_index = sys.argv.index("--package-second-selection")
                second = Rect(
                    float(sys.argv[second_index + 1]),
                    float(sys.argv[second_index + 2]),
                    float(sys.argv[second_index + 3]),
                    float(sys.argv[second_index + 4]),
                )
            except (ValueError, IndexError):
                LOGGER.error("--package-second-selection 参数无效")
                return 2
            window.preview.overlay.set_selections((selection, second))
            LOGGER.info("打包版多选框验收：共 2 个选框")
        window.output_dir_edit.setText(str(output.parent))
        window.output_name_edit.setText(output.name)
        if "--package-effect" in sys.argv:
            try:
                effect_index = sys.argv.index("--package-effect")
                effect_name = sys.argv[effect_index + 1]
                combo_index = window.effect_combo.findData(effect_name)
            except (ValueError, IndexError):
                LOGGER.error("--package-effect 参数无效")
                return 2
            if combo_index < 0:
                LOGGER.error("未知的打包验收处理模式: %s", effect_name)
                return 2
            window.effect_combo.setCurrentIndex(combo_index)
        if "--package-enhancement" in sys.argv:
            try:
                enhancement_index = sys.argv.index("--package-enhancement")
                enhancement_name = sys.argv[enhancement_index + 1]
                combo_index = window.enhancement_combo.findData(enhancement_name)
            except (ValueError, IndexError):
                LOGGER.error("--package-enhancement 参数无效")
                return 2
            if combo_index < 0:
                LOGGER.error("未知的打包验收超清模式: %s", enhancement_name)
                return 2
            window.enhancement_combo.setCurrentIndex(combo_index)

        timeout_timer = QTimer(window)
        timeout_timer.setSingleShot(True)

        def exit_when_idle(code: int) -> None:
            if (
                window.runner.running
                or window.smart_runner.running
                or window.vsr_runner.running
                or window.enhancement_runner.running
            ):
                QTimer.singleShot(100, lambda: exit_when_idle(code))
                return
            window.close()
            app.exit(code)

        def fail(code: int, message: str) -> None:
            LOGGER.error("打包版自动验收失败: %s", message)
            timeout_timer.stop()
            if window.enhancement_runner.running:
                window.enhancement_runner.cancel()
            elif window.vsr_runner.running:
                window.vsr_runner.cancel()
            elif window.smart_runner.running:
                window.smart_runner.cancel()
            elif window.runner.running:
                window.runner.cancel()
            exit_when_idle(code)

        def export_after_playback() -> None:
            if window.player.position() < 500:
                fail(3, f"播放器位置未前进: {window.player.position()} ms")
                return
            LOGGER.info("打包版播放验证通过，位置 %s ms", window.player.position())
            window.start_processing()

        def completed(output_text: str) -> None:
            result = Path(output_text)
            if result.resolve() != output.resolve():
                return
            if not result.is_file():
                fail(4, "导出完成信号触发但输出文件不存在")
                return
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(screenshot)):
                fail(5, "打包版界面截图保存失败")
                return
            LOGGER.info("打包版自动验收通过: %s", result)
            timeout_timer.stop()
            exit_when_idle(0)

        window.runner.finished.connect(completed)
        window.runner.errorOccurred.connect(lambda message: fail(6, message))
        window.smart_runner.completed.connect(completed)
        window.smart_runner.errorOccurred.connect(lambda message: fail(6, message))
        window.vsr_runner.completed.connect(completed)
        window.vsr_runner.errorOccurred.connect(lambda message: fail(6, message))
        window.enhancement_runner.completed.connect(completed)
        window.enhancement_runner.errorOccurred.connect(
            lambda message: fail(6, message)
        )
        window.player.play()
        QTimer.singleShot(2500, export_after_playback)
        timeout_timer.timeout.connect(lambda: fail(7, "自动验收超时"))
        timeout_timer.start(
            900_000
            if window.effect_combo.currentData() in {
                "ai_propainter",
                "ai_propainter_fast",
            }
            else 300_000
        )
    return app.exec()
