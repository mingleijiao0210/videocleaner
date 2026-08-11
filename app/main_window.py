"""视频文字区域去除工具中文主窗口。"""

from __future__ import annotations

import os
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .coordinate_mapper import Rect
from .enhancement_runner import EnhancementOptions, EnhancementRunner
from .ffmpeg_locator import require_ffmpeg
from .ffmpeg_runner import (
    ExportOptions,
    FFmpegRunner,
    generate_preview_frame,
    unique_output_path,
)
from .logger import LOGGER
from .media_info import MediaInfo, probe_media
from .settings import (
    APP_NAME,
    APP_VERSION,
    SUPPORTED_EXTENSIONS,
    VIDEO_FILTER,
    project_root,
    writable_root,
)
from .smart_text_processor import (
    SmartTextRunner,
    generate_smart_preview,
)
from .time_utils import format_player_time, format_time, parse_time
from .user_preferences import UserPreferences
from .video_widget import VideoPreview
from .vsr_runner import AI_EFFECT_MODES, VSRRunner


def _file_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} GB"


def _format_task_elapsed(seconds: float) -> str:
    tenths = max(0, round(float(seconds) * 10))
    hours, remainder = divmod(tenths, 36000)
    minutes, remainder = divmod(remainder, 600)
    whole_seconds, fraction = divmod(remainder, 10)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}.{fraction}"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.resize(1450, 900)
        self.setMinimumSize(1120, 720)
        self.paths = require_ffmpeg()
        self.media_info: MediaInfo | None = None
        self.current_file: Path | None = None
        self.last_output: Path | None = None
        self._slider_pressed = False
        self.automated_test = False
        self.preferences = UserPreferences()
        self._task_started_at: float | None = None
        self._processing_stage = "idle"
        self._enhancement_mode = "off"
        self._enhancement_final: Path | None = None
        self._enhancement_intermediate: Path | None = None

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(0.7)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.runner = FFmpegRunner(self.paths.ffmpeg, self)
        self.smart_runner = SmartTextRunner(self.paths.ffmpeg, self)
        self.vsr_runner = VSRRunner(self.paths.ffmpeg, self)
        self.enhancement_runner = EnhancementRunner(self.paths.ffmpeg, self)

        self._build_ui()
        self._task_timer = QTimer(self)
        self._task_timer.setInterval(100)
        self._task_timer.timeout.connect(self._update_task_elapsed)
        self.player.setVideoSink(self.preview.video_sink)
        self._connect_signals()
        self._set_processing(False)
        self.statusBar().showMessage("就绪。请选择一个视频。")
        LOGGER.info("程序启动，版本 %s", APP_VERSION)

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)

        header = QHBoxLayout()
        self.choose_video_button = QPushButton("选择视频")
        self.choose_video_button.setMinimumHeight(36)
        self.remove_video_button = QPushButton("移除当前视频")
        self.remove_video_button.setMinimumHeight(36)
        self.remove_video_button.setEnabled(False)
        self.path_label = QLabel("尚未选择视频")
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        header.addWidget(self.choose_video_button)
        header.addWidget(self.remove_video_button)
        header.addWidget(self.path_label, 1)
        root_layout.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root_layout.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.preview = VideoPreview()
        left_layout.addWidget(self.preview, 1)

        timeline_row = QHBoxLayout()
        self.current_time_label = QLabel("00:00:00")
        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 0)
        self.total_time_label = QLabel("00:00:00")
        timeline_row.addWidget(self.current_time_label)
        timeline_row.addWidget(self.timeline, 1)
        timeline_row.addWidget(self.total_time_label)
        left_layout.addLayout(timeline_row)

        playback = QHBoxLayout()
        self.play_button = QPushButton("播放")
        self.stop_button = QPushButton("停止")
        self.back_button = QPushButton("后退一秒")
        self.forward_button = QPushButton("前进一秒")
        self.jump_edit = QLineEdit("00:00:00.000")
        self.jump_edit.setMaximumWidth(125)
        self.jump_button = QPushButton("跳转")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(70)
        self.volume_slider.setMaximumWidth(120)
        self.mute_check = QCheckBox("静音")
        for widget in (
            self.play_button,
            self.stop_button,
            self.back_button,
            self.forward_button,
        ):
            playback.addWidget(widget)
        playback.addSpacing(8)
        playback.addWidget(QLabel("跳转到"))
        playback.addWidget(self.jump_edit)
        playback.addWidget(self.jump_button)
        playback.addStretch()
        playback.addWidget(QLabel("音量"))
        playback.addWidget(self.volume_slider)
        playback.addWidget(self.mute_check)
        left_layout.addLayout(playback)
        splitter.addWidget(left)

        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setMinimumWidth(390)
        right = QWidget()
        right_layout = QVBoxLayout(right)

        info_group = QGroupBox("视频信息")
        info_form = QFormLayout(info_group)
        self.info_values: dict[str, QLabel] = {}
        for key, title in (
            ("name", "文件名"),
            ("path", "文件路径"),
            ("resolution", "宽度和高度"),
            ("duration", "视频时长"),
            ("fps", "帧率"),
            ("vcodec", "视频编码"),
            ("acodec", "音频编码"),
            ("size", "文件大小"),
        ):
            label = QLabel("—")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.info_values[key] = label
            info_form.addRow(title, label)
        right_layout.addWidget(info_group)

        selection_group = QGroupBox("文字区域选框（原视频坐标）")
        selection_layout = QVBoxLayout(selection_group)
        coords = QHBoxLayout()
        self.coord_labels: dict[str, QLabel] = {}
        for name in ("X", "Y", "W", "H"):
            label = QLabel(f"{name}: —")
            self.coord_labels[name] = label
            coords.addWidget(label)
        selection_layout.addLayout(coords)
        self.selection_count_label = QLabel("选框数量：0（当前：—）")
        selection_layout.addWidget(self.selection_count_label)
        effect_row = QHBoxLayout()
        effect_row.addWidget(QLabel("处理效果"))
        self.effect_combo = QComboBox()
        model_root = (
            project_root()
            / "tools"
            / "vsr"
            / "engine"
            / "resources"
            / "backend"
            / "models"
        )
        propainter_models = (
            model_root / "video" / "ProPainter.pth",
            model_root / "video" / "raft-things.pth",
            model_root / "video" / "recurrent_flow_completion.pth",
        )
        sttn_model = model_root / "sttn" / "infer_model.pth"
        self.effect_combo.addItem(
            "本地 AI 真实背景融合（推荐，无整块马赛克）",
            "ai_strong",
        )
        if sys.platform == "darwin" and all(
            path.is_file() for path in propainter_models
        ):
            self.effect_combo.addItem(
                "ProPainter 时序修复（较慢，备用）",
                "ai_propainter_fast",
            )
            self.effect_combo.addItem(
                "ProPainter 专业时序修复（很慢）",
                "ai_propainter",
            )
        if sttn_model.is_file():
            self.effect_combo.addItem("本地 AI 极速去字（推荐低配电脑）", "ai_fast")
            self.effect_combo.addItem(
                "本地 AI 精准去字（高质量，保留边框）",
                "ai_precise",
            )
            self.effect_combo.addItem("本地 AI 整框重建（普通字幕）", "ai_full")
        self.effect_combo.addItem("快速笔画修补（低配实验）", "smart_text")
        self.effect_combo.addItem("传统自动（可能产生模糊）", "auto")
        self.effect_combo.addItem("纯白遮盖（完全隐藏）", "solid_cover")
        self.effect_combo.addItem("插值去除（小文字）", "delogo")
        self.effect_combo.addItem("柔和模糊（隐藏隐私信息）", "soft_blur")
        self.effect_combo.addItem("不去字（仅进行画面超清）", "none")
        self.effect_combo.setToolTip(
            "本地 AI 精准去字会先检测文字，再利用前后视频帧重建背景；"
            "整个过程完全在本机完成。"
        )
        self.effect_hint_label = QLabel(
            "推荐：只框住文字所在范围。程序检测文字笔画并利用前后帧恢复背景。"
        )
        self.effect_hint_label.setWordWrap(True)
        effect_row.addWidget(self.effect_combo, 1)
        selection_layout.addLayout(effect_row)
        selection_layout.addWidget(self.effect_hint_label)
        selection_buttons = QHBoxLayout()
        self.reselect_button = QPushButton("新增选框")
        self.delete_selection_button = QPushButton("删除当前选框")
        self.clear_selections_button = QPushButton("清空全部选框")
        self.preview_region_button = QPushButton("预览处理区域")
        selection_buttons.addWidget(self.reselect_button)
        selection_buttons.addWidget(self.delete_selection_button)
        selection_buttons.addWidget(self.clear_selections_button)
        selection_layout.addLayout(selection_buttons)
        selection_layout.addWidget(self.preview_region_button)
        hint = QLabel(
            "提示：直接在空白画面拖动或点击“新增选框”后拖动，可添加多处区域。"
            "点击任一选框可将它设为当前选框；拖动内部可移动，拖动边缘或角点可缩放。"
            "开始处理时会同时处理全部选框。"
        )
        hint.setWordWrap(True)
        selection_layout.addWidget(hint)
        right_layout.addWidget(selection_group)

        range_group = QGroupBox("处理时间范围")
        range_layout = QVBoxLayout(range_group)
        mode_row = QHBoxLayout()
        self.entire_radio = QRadioButton("处理整个视频")
        self.range_radio = QRadioButton("设置处理时间段")
        self.entire_radio.setChecked(True)
        self.range_group = QButtonGroup(self)
        self.range_group.addButton(self.entire_radio)
        self.range_group.addButton(self.range_radio)
        mode_row.addWidget(self.entire_radio)
        mode_row.addWidget(self.range_radio)
        range_layout.addLayout(mode_row)
        time_form = QFormLayout()
        self.start_time_edit = QLineEdit("00:00:00.000")
        self.end_time_edit = QLineEdit("00:00:00.000")
        self.start_time_edit.setEnabled(False)
        self.end_time_edit.setEnabled(False)
        time_form.addRow("开始时间", self.start_time_edit)
        time_form.addRow("结束时间", self.end_time_edit)
        range_layout.addLayout(time_form)
        right_layout.addWidget(range_group)

        output_group = QGroupBox("输出设置")
        output_layout = QFormLayout(output_group)
        output_dir_row = QHBoxLayout()
        self.output_dir_edit = QLineEdit(str(writable_root() / "output"))
        self.choose_output_button = QPushButton("选择输出位置")
        output_dir_row.addWidget(self.output_dir_edit, 1)
        output_dir_row.addWidget(self.choose_output_button)
        self.output_name_edit = QLineEdit("输出_已处理.mp4")
        self.enhancement_combo = QComboBox()
        self.enhancement_combo.addItem("关闭（保持现有画面）", "off")
        self.enhancement_combo.addItem(
            "快速清晰增强（推荐，保持原分辨率）",
            "clarity",
        )
        self.enhancement_combo.addItem(
            "本地 AI 超清 2×（较慢，最大 4K）",
            "ai_2x",
        )
        self.enhancement_combo.setToolTip(
            "快速档使用轻度时域降噪和细节恢复；AI 2× 使用本地 FSRCNN "
            "模型将宽高各放大两倍。两种模式都不上传视频。"
        )
        self.enhancement_hint_label = QLabel(
            "默认关闭，不影响现有去字效果。超清会处理整个导出视频。"
        )
        self.enhancement_hint_label.setWordWrap(True)
        output_layout.addRow("输出目录", output_dir_row)
        output_layout.addRow("输出文件名", self.output_name_edit)
        output_layout.addRow("画面超清增强", self.enhancement_combo)
        output_layout.addRow("", self.enhancement_hint_label)
        right_layout.addWidget(output_group)

        process_group = QGroupBox("处理进度")
        process_layout = QVBoxLayout(process_group)
        self.process_status_label = QLabel("等待开始")
        self.process_status_label.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_time_label = QLabel("已处理 00:00:00.000 / 00:00:00.000")
        self.task_elapsed_label = QLabel("任务用时：00:00:00.0")
        self.output_path_label = QLabel("输出文件：—")
        self.output_path_label.setWordWrap(True)
        self.output_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        process_layout.addWidget(self.process_status_label)
        process_layout.addWidget(self.progress_bar)
        process_layout.addWidget(self.progress_time_label)
        process_layout.addWidget(self.task_elapsed_label)
        process_layout.addWidget(self.output_path_label)
        action_row = QHBoxLayout()
        self.start_process_button = QPushButton("开始处理")
        self.start_process_button.setMinimumHeight(38)
        self.cancel_process_button = QPushButton("取消处理")
        self.open_output_button = QPushButton("打开输出文件夹")
        action_row.addWidget(self.start_process_button)
        action_row.addWidget(self.cancel_process_button)
        action_row.addWidget(self.open_output_button)
        process_layout.addLayout(action_row)
        right_layout.addWidget(process_group)
        right_layout.addStretch()

        right_scroll.setWidget(right)
        splitter.addWidget(right_scroll)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 2)

    def _connect_signals(self) -> None:
        self.choose_video_button.clicked.connect(self.choose_video)
        self.remove_video_button.clicked.connect(self.remove_current_video)
        self.play_button.clicked.connect(self._toggle_playback)
        self.stop_button.clicked.connect(self.player.stop)
        self.back_button.clicked.connect(lambda: self._seek_delta(-1000))
        self.forward_button.clicked.connect(lambda: self._seek_delta(1000))
        self.jump_button.clicked.connect(self.jump_to_time)
        self.timeline.sliderPressed.connect(self._timeline_pressed)
        self.timeline.sliderReleased.connect(self._timeline_released)
        self.timeline.sliderMoved.connect(self._timeline_preview)
        self.volume_slider.valueChanged.connect(
            lambda value: self.audio_output.setVolume(value / 100)
        )
        self.mute_check.toggled.connect(self.audio_output.setMuted)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.errorOccurred.connect(self._player_error)
        self.preview.selectionChanged.connect(self._selection_changed)
        self.effect_combo.currentIndexChanged.connect(self._update_effect_hint)
        self.enhancement_combo.currentIndexChanged.connect(
            self._update_enhancement_hint
        )
        self.reselect_button.clicked.connect(self.preview.overlay.begin_add_selection)
        self.delete_selection_button.clicked.connect(
            self.preview.overlay.delete_active_selection
        )
        self.clear_selections_button.clicked.connect(
            self.preview.overlay.clear_selection
        )
        self.preview_region_button.clicked.connect(self.preview_region)
        self.range_radio.toggled.connect(self._range_mode_changed)
        self.choose_output_button.clicked.connect(self.choose_output_directory)
        self.start_process_button.clicked.connect(self.start_processing)
        self.cancel_process_button.clicked.connect(self.cancel_processing)
        self.open_output_button.clicked.connect(self.open_output_directory)
        self.runner.progressChanged.connect(self._processing_progress)
        self.runner.statusChanged.connect(self.process_status_label.setText)
        self.runner.errorOccurred.connect(self._processing_error)
        self.runner.finished.connect(self._removal_finished)
        self.runner.cancelled.connect(self._processing_cancelled)
        self.smart_runner.progressChanged.connect(self._processing_progress)
        self.smart_runner.statusChanged.connect(self.process_status_label.setText)
        self.smart_runner.errorOccurred.connect(self._processing_error)
        self.smart_runner.completed.connect(self._removal_finished)
        self.smart_runner.cancelled.connect(self._processing_cancelled)
        self.vsr_runner.progressChanged.connect(self._processing_progress)
        self.vsr_runner.statusChanged.connect(self.process_status_label.setText)
        self.vsr_runner.errorOccurred.connect(self._processing_error)
        self.vsr_runner.completed.connect(self._removal_finished)
        self.vsr_runner.cancelled.connect(self._processing_cancelled)
        self.enhancement_runner.progressChanged.connect(self._processing_progress)
        self.enhancement_runner.statusChanged.connect(
            self.process_status_label.setText
        )
        self.enhancement_runner.errorOccurred.connect(self._processing_error)
        self.enhancement_runner.completed.connect(self._processing_finished)
        self.enhancement_runner.cancelled.connect(self._processing_cancelled)

    def _set_processing(self, processing: bool) -> None:
        self.start_process_button.setEnabled(not processing)
        self.cancel_process_button.setEnabled(processing)
        self.choose_video_button.setEnabled(not processing)
        self.remove_video_button.setEnabled(
            not processing and self.media_info is not None
        )
        self.choose_output_button.setEnabled(not processing)
        self.output_dir_edit.setEnabled(not processing)
        self.output_name_edit.setEnabled(not processing)
        self.effect_combo.setEnabled(not processing)
        self.enhancement_combo.setEnabled(not processing)

    def _start_task_timer(self) -> None:
        self._task_started_at = time.monotonic()
        self.task_elapsed_label.setText("任务用时：00:00:00.0")
        self._task_timer.start()

    def _update_task_elapsed(self) -> None:
        if self._task_started_at is None:
            return
        elapsed = time.monotonic() - self._task_started_at
        self.task_elapsed_label.setText(
            f"任务用时：{_format_task_elapsed(elapsed)}"
        )

    def _stop_task_timer(self, result: str) -> None:
        if self._task_started_at is None:
            return
        elapsed = time.monotonic() - self._task_started_at
        self._task_timer.stop()
        self._task_started_at = None
        self.task_elapsed_label.setText(
            f"任务用时：{_format_task_elapsed(elapsed)}（{result}）"
        )

    def choose_video(self) -> None:
        initial_directory = self.preferences.last_input_directory(
            project_root()
        )
        path_text, _ = QFileDialog.getOpenFileName(
            self, "选择视频", str(initial_directory), VIDEO_FILTER
        )
        if path_text:
            selected = Path(path_text)
            self.preferences.set_last_input_directory(selected.parent)
            self.load_video(selected)

    def load_video(self, path: Path) -> None:
        try:
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                raise ValueError("请选择 MP4、MOV、AVI、MKV、M4V 或 WebM 视频。")
            info = probe_media(self.paths.ffprobe, path)
            if info.width <= 0 or info.height <= 0 or info.duration <= 0:
                raise ValueError("视频分辨率或时长无效。")
            self.media_info = info
            self.current_file = info.path
            self.path_label.setText(str(info.path))
            self._show_media_info(info)
            self.preview.set_video_size(info.width, info.height)
            self.preview.overlay.clear_selection()
            self.player.setSource(QUrl.fromLocalFile(str(info.path)))
            self.output_name_edit.setText(f"{info.path.stem}_已处理.mp4")
            self.end_time_edit.setText(format_time(info.duration))
            self.progress_time_label.setText(
                f"已处理 {format_time(0)} / {format_time(info.duration)}"
            )
            self.statusBar().showMessage("视频已导入，可以播放并框选文字区域。")
            self.remove_video_button.setEnabled(True)
            LOGGER.info("视频导入: %s", info.path)
            LOGGER.info(
                "视频信息: %sx%s, 时长 %.3f, 视频 %s, 音频 %s",
                info.width,
                info.height,
                info.duration,
                info.video_codec,
                info.audio_codec or "无",
            )
        except Exception as exc:
            LOGGER.exception("视频导入失败")
            QMessageBox.warning(self, "无法导入视频", str(exc).splitlines()[0])

    def _show_media_info(self, info: MediaInfo) -> None:
        values = {
            "name": info.path.name,
            "path": str(info.path),
            "resolution": f"{info.width} × {info.height}",
            "duration": format_time(info.duration),
            "fps": f"{info.frame_rate:.3f} fps（{info.frame_rate_text}）",
            "vcodec": info.video_codec,
            "acodec": info.audio_codec or "无音频",
            "size": _file_size(info.file_size),
        }
        for key, value in values.items():
            self.info_values[key].setText(value)

    def _position_changed(self, position: int) -> None:
        if not self._slider_pressed:
            self.timeline.setValue(position)
        self.current_time_label.setText(format_player_time(position))

    def _duration_changed(self, duration: int) -> None:
        self.timeline.setRange(0, max(0, duration))
        self.total_time_label.setText(format_player_time(duration))

    def _timeline_pressed(self) -> None:
        self._slider_pressed = True

    def _timeline_released(self) -> None:
        self._slider_pressed = False
        self.player.setPosition(self.timeline.value())

    def _timeline_preview(self, value: int) -> None:
        self.current_time_label.setText(format_player_time(value))

    def _seek_delta(self, delta: int) -> None:
        self.player.setPosition(
            min(max(self.player.position() + delta, 0), self.player.duration())
        )

    def _toggle_playback(self) -> None:
        if (
            self.player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
        ):
            self.player.pause()
        else:
            self.player.play()

    def _playback_state_changed(
        self,
        state: QMediaPlayer.PlaybackState,
    ) -> None:
        self.play_button.setText(
            "暂停"
            if state == QMediaPlayer.PlaybackState.PlayingState
            else "播放"
        )

    def jump_to_time(self) -> None:
        try:
            seconds = parse_time(self.jump_edit.text())
            if self.media_info and seconds > self.media_info.duration:
                raise ValueError("跳转时间不能超过视频总时长。")
            self.player.setPosition(round(seconds * 1000))
        except ValueError as exc:
            QMessageBox.information(self, "时间格式有误", str(exc))

    def _player_error(self, _error, error_string: str = "") -> None:
        if error_string:
            LOGGER.error("播放器错误: %s", error_string)
            self.statusBar().showMessage(f"播放器提示：{error_string}")

    def _selection_changed(self, rect: Rect | None) -> None:
        selections = self.preview.overlay.selections
        active_number = (
            self.preview.overlay.active_index + 1
            if rect is not None
            else None
        )
        self.selection_count_label.setText(
            f"选框数量：{len(selections)}（当前："
            f"{active_number if active_number is not None else '—'}）"
        )
        if rect is None:
            for name, label in self.coord_labels.items():
                label.setText(f"{name}: —")
            self._update_effect_hint()
            return
        values = {
            "X": round(rect.x),
            "Y": round(rect.y),
            "W": round(rect.width),
            "H": round(rect.height),
        }
        for name, value in values.items():
            self.coord_labels[name].setText(f"{name}: {value}")
        self._update_effect_hint()

    def _update_effect_hint(self, _value=None) -> None:
        mode = self.effect_combo.currentData()
        if mode == "none":
            self.effect_hint_label.setText(
                "不会去除任何文字，也不需要画选框；请在“输出设置”中选择一种"
                "画面超清增强。"
            )
            return
        if mode == "ai_propainter_fast":
            self.effect_hint_label.setText(
                "备用慢速模式：使用 ProPainter、RAFT 光流和前后帧时序重建。"
                "复杂素材可能耗时数分钟；新版只融合文字笔画，不再替换整块选区。"
            )
            return
        if mode == "ai_propainter":
            self.effect_hint_label.setText(
                "M2 Pro 最高质量模式：使用 Metal/MPS 加速 ProPainter 光流与时序传播，"
                "利用前后帧重建文字后的运动背景。32GB 统一内存会启用高质量裁剪；"
                "速度慢于强力模式，但复杂纹理和运动画面通常更自然。"
            )
            return
        if mode == "ai_strong":
            self.effect_hint_label.setText(
                "推荐真实背景融合：把 OCR 方框收紧为白色、黄色等实际文字笔画，"
                "只重建文字和描边覆盖的像素，选区内其他草地、水面和纹理保持原画；"
                "M2 Pro 会按 24 帧安全批次处理、后台预解码并复用连续字幕遮罩；"
                "视频由 VideoToolbox 单次硬件编码。"
                "请只框住字幕所在的横向区域；"
                "完全离线，不上传视频。"
            )
            return
        if mode == "ai_fast":
            self.effect_hint_label.setText(
                "增强极速模式：提高内部重建清晰度，完整保留 8 层时序建模，"
                "并与周围画面柔和融合。"
                "适合固定字幕和文字框；复杂运动背景可改用“本地 AI 精准去字”。"
            )
            return
        if mode == "ai_precise":
            self.effect_hint_label.setText(
                "推荐模式：在选框内检测文字笔画，再用前后视频帧重建被遮挡的背景。"
                "文字框边缘、白底和其他未识别为文字的内容会尽量保留；完全离线。"
            )
            return
        if mode == "ai_full":
            self.effect_hint_label.setText(
                "将整个选框视为遮挡区，用前后帧重建。适合底部普通字幕；"
                "若选框包含需要保留的边框，请改用“本地 AI 精准去字”。"
            )
            return
        if mode == "smart_text":
            self.effect_hint_label.setText(
                "低配快速模式，只使用传统图像算法修补文字笔画。速度快，"
                "复杂背景可能出现模糊或拉伸痕迹。"
            )
            return
        if mode == "delogo":
            self.effect_hint_label.setText(
                "将使用周边像素插值；适合较小的固定文字区域。"
            )
            return
        if mode == "solid_cover":
            self.effect_hint_label.setText(
                "将使用白色圆角遮罩完全盖住框内内容；不会恢复原背景。"
            )
            return
        if mode == "soft_blur":
            self.effect_hint_label.setText(
                "将使用强高斯模糊和羽化边缘；大区域痕迹更自然，但不能恢复原画面。"
            )
            return
        rect = self.preview.overlay.selection
        if not rect or not self.media_info:
            self.effect_hint_label.setText("自动模式会根据选框大小选择效果")
            return
        width_ratio = rect.width / self.media_info.width
        height_ratio = rect.height / self.media_info.height
        area_ratio = rect.width * rect.height / (
            self.media_info.width * self.media_info.height
        )
        if width_ratio >= 0.60 or height_ratio >= 0.20 or area_ratio >= 0.08:
            self.effect_hint_label.setText(
                "自动选择：柔和模糊（当前选框面积较大，可减少拉伸条纹）"
            )
        else:
            self.effect_hint_label.setText(
                "自动选择：插值去除（当前选框较小）"
            )

    def _update_enhancement_hint(self, _value=None) -> None:
        mode = self.enhancement_combo.currentData()
        if mode == "ai_2x":
            self.enhancement_hint_label.setText(
                "本地 AI 将宽和高各放大 2 倍，适合低分辨率、轻度模糊视频。"
                "处理较慢，输出不超过 4K；无法凭空恢复原视频从未记录的细节。"
            )
        elif mode == "clarity":
            self.enhancement_hint_label.setText(
                "轻度时域降噪并恢复边缘细节，保持原分辨率，使用本机硬件编码；"
                "速度明显快于 AI 2×。"
            )
        else:
            self.enhancement_hint_label.setText(
                "默认关闭，不影响现有去字效果。超清会处理整个导出视频。"
            )

    def _range_mode_changed(self, enabled: bool) -> None:
        self.start_time_edit.setEnabled(enabled)
        self.end_time_edit.setEnabled(enabled)

    def choose_output_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择输出位置", self.output_dir_edit.text()
        )
        if directory:
            self.output_dir_edit.setText(directory)

    def _export_options(self, output_path: Path) -> ExportOptions:
        if not self.media_info or not self.current_file:
            raise ValueError("请先选择视频。")
        selections = self.preview.overlay.selections
        effect_mode = self.effect_combo.currentData()
        if not selections and effect_mode != "none":
            raise ValueError("请先在视频画面中框选要处理的文字区域。")
        if not selections:
            selections = (
                Rect(
                    0.0,
                    0.0,
                    float(self.media_info.width),
                    float(self.media_info.height),
                ),
            )
        start = end = None
        if self.range_radio.isChecked():
            start = parse_time(self.start_time_edit.text())
            end = parse_time(self.end_time_edit.text())
        return ExportOptions(
            input_path=self.current_file,
            output_path=output_path,
            selection=selections[0],
            video_width=self.media_info.width,
            video_height=self.media_info.height,
            duration=self.media_info.duration,
            audio_codec=self.media_info.audio_codec,
            selections=selections,
            range_start=start,
            range_end=end,
            effect_mode=effect_mode,
            enhancement_mode=self.enhancement_combo.currentData(),
        )

    def _requested_output_path(self) -> Path:
        directory = Path(self.output_dir_edit.text().strip())
        name = self.output_name_edit.text().strip()
        if not name:
            raise ValueError("请输入输出文件名。")
        if Path(name).name != name:
            raise ValueError("输出文件名不能包含目录。")
        if not name.lower().endswith(".mp4"):
            name += ".mp4"
            self.output_name_edit.setText(name)
        return unique_output_path(directory / name)

    def start_processing(self) -> None:
        try:
            output_path = self._requested_output_path()
            options = self._export_options(output_path)
            if (
                options.effect_mode == "none"
                and options.enhancement_mode == "off"
            ):
                raise ValueError(
                    "当前既未选择去字，也未开启画面超清，请至少选择一项处理功能。"
                )
            self.player.pause()
            self.progress_bar.setValue(0)
            self.output_path_label.setText(f"输出文件：{output_path}")
            self.process_status_label.setText("正在启动 FFmpeg…")
            self._set_processing(True)
            self._start_task_timer()
            LOGGER.info("处理开始，输出: %s", output_path)
            self._enhancement_mode = options.enhancement_mode
            self._enhancement_final = (
                output_path if options.enhancement_mode != "off" else None
            )
            self._enhancement_intermediate = None
            if options.effect_mode == "none":
                self._processing_stage = "enhancement_only"
                self._start_enhancement(
                    options.input_path,
                    output_path,
                    options,
                )
                return
            removal_options = options
            if options.enhancement_mode != "off":
                intermediate = output_path.parent / (
                    f".videocleaner_pre_enhance_{uuid.uuid4().hex}.mp4"
                )
                self._enhancement_intermediate = intermediate
                removal_options = replace(options, output_path=intermediate)
            self._processing_stage = "removal"
            if removal_options.effect_mode in AI_EFFECT_MODES:
                self.process_status_label.setText("正在启动本地 AI 时序修复…")
                self.vsr_runner.start_with(removal_options)
            elif removal_options.effect_mode == "smart_text":
                self.process_status_label.setText("正在启动智能精确去字…")
                self.smart_runner.start_with(removal_options)
            else:
                self.runner.start(removal_options)
        except Exception as exc:
            self._cleanup_enhancement_intermediate()
            self._stop_task_timer("未开始")
            self._set_processing(False)
            LOGGER.exception("无法开始处理")
            QMessageBox.warning(self, "无法开始处理", str(exc).splitlines()[0])

    def _start_enhancement(
        self,
        source: Path,
        output: Path,
        options: ExportOptions,
    ) -> None:
        mode = options.enhancement_mode
        if mode == "off":
            raise ValueError("尚未选择画面超清模式。")
        self.enhancement_runner.start_with(
            EnhancementOptions(
                input_path=source,
                output_path=output,
                mode=mode,
                duration=options.duration,
                width=options.video_width,
                height=options.video_height,
                audio_codec=options.audio_codec,
            )
        )

    def _removal_finished(self, output_text: str) -> None:
        if self._enhancement_mode == "off":
            self._processing_finished(output_text)
            return
        if not self.media_info or not self.current_file or not self._enhancement_final:
            self._processing_error("画面增强启动失败：任务状态不完整。")
            return
        intermediate = Path(output_text)
        try:
            self._processing_stage = "enhancement_after_removal"
            self.process_status_label.setText("去字完成，正在继续进行画面超清增强…")
            options = self._export_options(self._enhancement_final)
            self._start_enhancement(
                intermediate,
                self._enhancement_final,
                options,
            )
        except Exception as exc:
            LOGGER.exception("无法启动去字后的画面增强")
            self._processing_error(str(exc).splitlines()[0])

    def cancel_processing(self) -> None:
        if self.enhancement_runner.running:
            self.enhancement_runner.cancel()
        elif self.vsr_runner.running:
            self.vsr_runner.cancel()
        elif self.smart_runner.running:
            self.smart_runner.cancel()
        else:
            self.runner.cancel()

    def _processing_progress(
        self, percent: float, processed: float, total: float
    ) -> None:
        shown_percent = percent
        if self._enhancement_mode != "off":
            if self._processing_stage == "removal":
                shown_percent = percent * 0.70
            elif self._processing_stage == "enhancement_after_removal":
                shown_percent = 70.0 + percent * 0.30
        self.progress_bar.setValue(round(shown_percent * 10))
        self.progress_time_label.setText(
            f"已处理 {format_time(processed)} / {format_time(total)}"
        )

    def _processing_error(self, message: str) -> None:
        self._cleanup_enhancement_intermediate()
        self._processing_stage = "idle"
        self._stop_task_timer("失败")
        self._set_processing(False)
        self.process_status_label.setText(message)
        if not self.automated_test:
            QMessageBox.critical(self, "处理失败", message)

    def _unload_current_video(self) -> None:
        """Release the completed input so the next video can be selected immediately."""

        self.player.stop()
        self.player.setSource(QUrl())
        self.preview.overlay.clear_selection()
        self.preview.frame_widget.clear()
        self.preview.set_video_size(0, 0)
        self.media_info = None
        self.current_file = None
        self.remove_video_button.setEnabled(False)
        self.path_label.setText("尚未选择视频")
        for label in self.info_values.values():
            label.setText("—")
        self.timeline.setRange(0, 0)
        self.timeline.setValue(0)
        self.current_time_label.setText("00:00:00")
        self.total_time_label.setText("00:00:00")
        self.jump_edit.setText("00:00:00.000")
        self.entire_radio.setChecked(True)
        self.start_time_edit.setText("00:00:00.000")
        self.end_time_edit.setText("00:00:00.000")
        self.output_name_edit.setText("输出_已处理.mp4")
        self.statusBar().showMessage("处理完成，当前视频已卸载，可以选择下一个视频。")
        LOGGER.info("当前输入视频已从界面卸载")

    def remove_current_video(self) -> None:
        """Remove a mistakenly selected video from the UI without deleting it."""

        if self.media_info is None:
            return
        source = self.current_file
        self._unload_current_video()
        self.progress_bar.setValue(0)
        self.progress_time_label.setText(
            f"已处理 {format_time(0)} / {format_time(0)}"
        )
        self.process_status_label.setText("当前视频已移除，可以重新选择视频")
        self.statusBar().showMessage("当前视频已移除，可以重新选择视频。")
        LOGGER.info("用户从界面移除当前视频（未删除原文件）: %s", source)

    def _processing_finished(self, output_text: str) -> None:
        self._cleanup_enhancement_intermediate()
        self._processing_stage = "idle"
        self._stop_task_timer("完成")
        self._set_processing(False)
        self.last_output = Path(output_text)
        self.process_status_label.setText("处理完成，可以选择下一个视频")
        self.output_path_label.setText(f"输出文件：{output_text}")
        self._unload_current_video()
        if not self.automated_test:
            QMessageBox.information(
                self,
                "处理完成",
                f"视频已保存到：\n{output_text}\n\n"
                "当前视频已从软件中自动卸载，可以继续选择下一个视频。",
            )

    def _processing_cancelled(self) -> None:
        self._cleanup_enhancement_intermediate()
        self._processing_stage = "idle"
        self._stop_task_timer("已取消")
        self._set_processing(False)
        self.progress_bar.setValue(0)
        self.process_status_label.setText("处理已取消，未完成文件已清理。")

    def _cleanup_enhancement_intermediate(self) -> None:
        intermediate = self._enhancement_intermediate
        self._enhancement_intermediate = None
        self._enhancement_final = None
        self._enhancement_mode = "off"
        if intermediate is not None:
            intermediate.unlink(missing_ok=True)

    def preview_region(self) -> None:
        try:
            if not self.media_info:
                raise ValueError("请先选择视频。")
            output = writable_root() / "output" / ".preview_region.png"
            options = self._export_options(output.with_suffix(".mp4"))
            if options.effect_mode == "none":
                raise ValueError(
                    "仅做画面超清时不需要预览选框，直接点击“开始处理”即可。"
                )
            if options.effect_mode in {
                "ai_propainter_fast",
                "ai_propainter",
                "ai_strong",
                "ai_fast",
                "ai_precise",
            }:
                generate_smart_preview(
                    options.input_path,
                    self.player.position() / 1000,
                    options.selections or (options.selection,),
                    output,
                )
            elif options.effect_mode == "ai_full":
                generate_preview_frame(
                    self.paths.ffmpeg,
                    replace(options, effect_mode="delogo"),
                    self.player.position() / 1000,
                    output,
                )
            else:
                generate_preview_frame(
                    self.paths.ffmpeg,
                    options,
                    self.player.position() / 1000,
                    output,
                )
            pixmap = QPixmap(str(output))
            if pixmap.isNull():
                raise RuntimeError("预览图无法显示。")
            dialog = QDialog(self)
            if options.effect_mode == "ai_propainter_fast":
                dialog.setWindowTitle(
                    "M2 Pro 高质量快速预览（绿色为预计文字笔画，实际按时序重建）"
                )
            elif options.effect_mode == "ai_propainter":
                dialog.setWindowTitle(
                    "M2 Pro ProPainter 预览（绿色为预计文字笔画，实际按时序重建）"
                )
            elif options.effect_mode == "ai_strong":
                dialog.setWindowTitle(
                    "AI 强力模式预览（绿色为预计文字笔画，实际以 AI 检测为准）"
                )
            elif options.effect_mode == "ai_fast":
                dialog.setWindowTitle(
                    "AI 极速模式预览（绿色为预计文字笔画，实际以 AI 检测为准）"
                )
            elif options.effect_mode == "ai_precise":
                dialog.setWindowTitle(
                    "AI 精准模式预览（绿色为预计文字笔画，实际以 AI 检测为准）"
                )
            elif options.effect_mode == "ai_full":
                dialog.setWindowTitle("AI 整框重建预览（绿色边框内将被重建）")
            elif options.effect_mode == "smart_text":
                dialog.setWindowTitle("快速修补预览（绿色笔画是实际修复范围）")
            else:
                dialog.setWindowTitle("处理区域预览（绿色边框内将被处理）")
            layout = QVBoxLayout(dialog)
            label = QLabel()
            label.setPixmap(
                pixmap.scaled(
                    1000,
                    700,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            layout.addWidget(label)
            close_button = QPushButton("关闭")
            close_button.clicked.connect(dialog.accept)
            layout.addWidget(close_button)
            dialog.exec()
        except Exception as exc:
            LOGGER.exception("处理区域预览失败")
            QMessageBox.warning(self, "无法预览", str(exc).splitlines()[0])

    def open_output_directory(self) -> None:
        path = self.last_output.parent if self.last_output else Path(
            self.output_dir_edit.text().strip()
        )
        if not path.is_dir():
            QMessageBox.information(self, "目录不存在", "输出目录尚不存在。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def closeEvent(self, event: QCloseEvent) -> None:
        if (
            self.runner.running
            or self.smart_runner.running
            or self.vsr_runner.running
            or self.enhancement_runner.running
        ):
            answer = QMessageBox.question(
                self,
                "处理尚未完成",
                "关闭软件会取消当前处理任务，确定要关闭吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self.enhancement_runner.running:
                self.enhancement_runner.cancel()
                self.enhancement_runner.wait(8000)
            elif self.vsr_runner.running:
                self.vsr_runner.cancel()
                self.vsr_runner.wait(8000)
            elif self.smart_runner.running:
                self.smart_runner.cancel()
                self.smart_runner.wait(5000)
            else:
                self.runner.cancel()
                if not self.runner.process.waitForFinished(3000):
                    self.runner.process.kill()
                    self.runner.process.waitForFinished(2000)
        self.player.stop()
        LOGGER.info("程序关闭")
        event.accept()
