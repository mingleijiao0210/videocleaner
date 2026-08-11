from pathlib import Path

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QFileDialog, QMessageBox

from app.coordinate_mapper import Rect
from app.ffmpeg_locator import locate_ffmpeg
from app.main_window import MainWindow, _format_task_elapsed
from app.user_preferences import UserPreferences


pytestmark = pytest.mark.skipif(
    locate_ffmpeg() is None,
    reason="GUI workflow tests require local FFmpeg and ffprobe",
)


def test_play_pause_uses_one_toggle_button(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    assert not hasattr(window, "pause_button")
    assert window.play_button.text() == "播放"
    window._playback_state_changed(QMediaPlayer.PlaybackState.PlayingState)
    assert window.play_button.text() == "暂停"
    window._playback_state_changed(QMediaPlayer.PlaybackState.PausedState)
    assert window.play_button.text() == "播放"


def test_recommended_mode_and_elapsed_timer_are_visible(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)

    assert window.effect_combo.currentData() == "ai_strong"
    assert "真实背景融合" in window.effect_combo.currentText()
    assert window.enhancement_combo.currentData() == "off"
    assert window.enhancement_combo.findData("clarity") >= 0
    assert window.enhancement_combo.findData("ai_2x") >= 0
    assert window.task_elapsed_label.text() == "任务用时：00:00:00.0"
    assert _format_task_elapsed(65.49) == "00:01:05.5"

    window._start_task_timer()
    window._task_started_at -= 65.49
    window._update_task_elapsed()
    assert window.task_elapsed_label.text() == "任务用时：00:01:05.5"
    window._stop_task_timer("完成")
    assert window.task_elapsed_label.text().endswith("（完成）")


def test_enhancement_only_does_not_require_a_selection(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    source = Path(__file__).parent.parent / "test_media" / "vsr_short_test.mp4"
    window.load_video(source)
    window.effect_combo.setCurrentIndex(window.effect_combo.findData("none"))
    window.enhancement_combo.setCurrentIndex(
        window.enhancement_combo.findData("clarity")
    )

    options = window._export_options(tmp_path / "仅超清.mp4")

    assert options.effect_mode == "none"
    assert options.enhancement_mode == "clarity"
    assert options.selection == Rect(
        0.0,
        0.0,
        float(window.media_info.width),
        float(window.media_info.height),
    )


def test_remove_wrong_video_unloads_ui_but_keeps_source(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    source = Path(__file__).parent.parent / "test_media" / "vsr_short_test.mp4"
    window.load_video(source)
    assert window.media_info is not None
    assert window.remove_video_button.isEnabled()

    window.remove_video_button.click()

    assert window.media_info is None
    assert window.current_file is None
    assert window.preview.overlay.selection is None
    assert window.path_label.text() == "尚未选择视频"
    assert not window.remove_video_button.isEnabled()
    assert source.is_file()


def test_gui_exports_all_created_selections(qtbot, tmp_path):
    window = MainWindow()
    qtbot.addWidget(window)
    source = Path(__file__).parent.parent / "test_media" / "vsr_short_test.mp4"
    window.load_video(source)
    first = Rect(20, 20, 100, 40)
    second = Rect(300, 200, 120, 50)
    window.preview.overlay.set_selections((first, second))

    options = window._export_options(tmp_path / "多选框.mp4")

    assert options.selections == (first, second)
    assert options.selection == first
    assert window.selection_count_label.text() == "选框数量：2（当前：2）"


def test_video_dialog_starts_in_and_remembers_last_directory(
    qtbot, monkeypatch, tmp_path
):
    window = MainWindow()
    qtbot.addWidget(window)
    settings_path = tmp_path / "settings" / "user_preferences.json"
    window.preferences = UserPreferences(settings_path)
    first_directory = tmp_path / "首次目录"
    first_directory.mkdir()
    window.preferences.set_last_input_directory(first_directory)
    source = Path(__file__).parent.parent / "test_media" / "vsr_short_test.mp4"
    captured = {}

    def fake_dialog(_parent, _title, initial, _filter):
        captured["initial"] = initial
        return str(source), ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", fake_dialog)
    window.choose_video()

    assert Path(captured["initial"]) == first_directory.resolve()
    assert (
        UserPreferences(settings_path).last_input_directory(tmp_path)
        == source.parent.resolve()
    )
    assert window.current_file == source.resolve()


@pytest.mark.integration
def test_gui_load_and_async_export(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)
    window = MainWindow()
    qtbot.addWidget(window)
    source = Path(__file__).parent.parent / "test_media" / "横屏测试视频.mp4"
    if not source.exists():
        pytest.skip("测试媒体尚未生成")
    window.load_video(source)
    assert window.media_info is not None
    assert window.info_values["resolution"].text() == "1280 × 720"
    window.preview.overlay.set_selection(Rect(430, 610, 420, 70))
    window.effect_combo.setCurrentIndex(window.effect_combo.findData("delogo"))
    window.output_dir_edit.setText(str(tmp_path))
    window.output_name_edit.setText("界面异步导出.mp4")
    with qtbot.waitSignal(window.runner.finished, timeout=120_000) as signal:
        window.start_processing()
    assert Path(signal.args[0]).is_file()
    assert window.progress_bar.value() == 1000
    assert window.start_process_button.isEnabled()
    assert window.media_info is None
    assert window.current_file is None
    assert window.preview.overlay.selection is None
    assert window.path_label.text() == "尚未选择视频"


@pytest.mark.integration
def test_async_cancel_removes_partial_file(qtbot, monkeypatch, tmp_path):
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: None)
    window = MainWindow()
    qtbot.addWidget(window)
    source = Path(__file__).parent.parent / "test_media" / "竖屏测试视频.mp4"
    if not source.exists():
        pytest.skip("测试媒体尚未生成")
    window.load_video(source)
    window.preview.overlay.set_selection(Rect(100, 1120, 520, 90))
    window.effect_combo.setCurrentIndex(window.effect_combo.findData("delogo"))
    window.output_dir_edit.setText(str(tmp_path))
    window.output_name_edit.setText("应被取消.mp4")
    with qtbot.waitSignal(window.runner.cancelled, timeout=30_000):
        window.start_processing()
        QTimer.singleShot(20, window.runner.cancel)
    assert not (tmp_path / "应被取消.mp4").exists()
    assert not list(tmp_path.glob(".videocleaner_cover_*.png"))
    assert not window.runner.running
