"""FFmpeg 命令生成、异步执行和进度解析。"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import uuid
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter

from .coordinate_mapper import Rect, adjust_rect_for_yuv420p
from .logger import LOGGER
from .media_info import MediaInfo
from .settings import DEFAULT_CRF, DEFAULT_PRESET, MIN_SELECTION_SIZE

MP4_COPY_AUDIO_CODECS = {"aac", "mp3", "ac3", "eac3", "alac"}


@dataclass(frozen=True)
class ExportOptions:
    input_path: Path
    output_path: Path
    selection: Rect
    video_width: int
    video_height: int
    duration: float
    audio_codec: str | None
    selections: tuple[Rect, ...] = ()
    range_start: float | None = None
    range_end: float | None = None
    crf: int = DEFAULT_CRF
    preset: str = DEFAULT_PRESET
    force_aac: bool = False
    effect_mode: str = "auto"
    enhancement_mode: str = "off"
    cover_image_path: Path | None = None


def all_selections(options: ExportOptions) -> tuple[Rect, ...]:
    return options.selections or (options.selection,)


def validate_export(options: ExportOptions, ffmpeg: Path) -> None:
    if not Path(ffmpeg).is_file():
        raise FileNotFoundError("FFmpeg 不存在。")
    if not options.input_path.is_file():
        raise FileNotFoundError("输入视频不存在。")
    if options.input_path.resolve() == options.output_path.resolve():
        raise ValueError("输出文件不能覆盖原视频。")
    if options.video_width <= 0 or options.video_height <= 0:
        raise ValueError("视频分辨率无效。")
    selections = all_selections(options)
    if not selections:
        raise ValueError("请至少创建一个选框。")
    for index, clean in enumerate(selections, start=1):
        if clean.width < MIN_SELECTION_SIZE or clean.height < MIN_SELECTION_SIZE:
            raise ValueError(
                f"第 {index} 个选框宽度和高度不能小于 "
                f"{MIN_SELECTION_SIZE} 像素。"
            )
        if clean.x < 0 or clean.y < 0:
            raise ValueError(f"第 {index} 个选框坐标不能小于 0。")
        if (
            clean.right > options.video_width
            or clean.bottom > options.video_height
        ):
            raise ValueError(f"第 {index} 个选框不能超出视频边界。")
    if (options.range_start is None) != (options.range_end is None):
        raise ValueError("开始时间和结束时间必须同时设置。")
    if options.range_start is not None:
        if options.range_start < 0 or options.range_end is None:
            raise ValueError("处理时间不能小于 0。")
        if options.range_start >= options.range_end:
            raise ValueError("开始时间必须早于结束时间。")
        if options.range_end > options.duration + 0.001:
            raise ValueError("结束时间不能超过视频总时长。")
    parent = options.output_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.NamedTemporaryFile(dir=parent, prefix=".write_test_", delete=True):
            pass
    except OSError as exc:
        raise PermissionError("输出目录不可写，请选择其他位置。") from exc


def build_delogo_filter(options: ExportOptions, show: bool = False) -> str:
    filters = []
    for selection in all_selections(options):
        x, y, width, height = adjust_rect_for_yuv420p(
            selection,
            options.video_width,
            options.video_height,
        )
        value = (
            f"delogo=x={x}:y={y}:w={width}:h={height}:"
            f"show={1 if show else 0}"
        )
        if options.range_start is not None and options.range_end is not None:
            value += (
                f":enable='between(t,{options.range_start:.3f},"
                f"{options.range_end:.3f})'"
            )
        filters.append(value)
    return ",".join(filters)


def resolve_effect_mode(options: ExportOptions) -> str:
    """大区域自动柔和模糊，小区域使用 delogo 插值。"""
    if options.effect_mode in {"delogo", "soft_blur", "solid_cover"}:
        return options.effect_mode
    if options.effect_mode != "auto":
        raise ValueError("未知的处理效果模式。")
    for selection in all_selections(options):
        width_ratio = selection.width / max(options.video_width, 1)
        height_ratio = selection.height / max(options.video_height, 1)
        area_ratio = (
            selection.width
            * selection.height
            / max(options.video_width * options.video_height, 1)
        )
        if (
            width_ratio >= 0.60
            or height_ratio >= 0.20
            or area_ratio >= 0.08
        ):
            return "soft_blur"
    return "delogo"


def create_rounded_cover_image(path: Path, width: int, height: int) -> Path:
    """创建透明背景、白色圆角矩形 PNG，覆盖区内部完全不透明。"""
    if width <= 0 or height <= 0:
        raise ValueError("遮盖区域尺寸无效。")
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255, 255))
    radius = max(4.0, min(24.0, width / 6.0, height / 3.0))
    inset = 0.5
    painter.drawRoundedRect(
        QRectF(inset, inset, width - 1.0, height - 1.0), radius, radius
    )
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path), "PNG"):
        raise RuntimeError("无法创建白色遮盖图。")
    return path


def create_multi_cover_image(
    path: Path,
    video_width: int,
    video_height: int,
    selections: tuple[Rect, ...],
) -> Path:
    """Create one transparent full-frame overlay containing all rounded covers."""

    image = QImage(video_width, video_height, QImage.Format.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255, 255))
    for selection in selections:
        x, y, width, height = adjust_rect_for_yuv420p(
            selection,
            video_width,
            video_height,
        )
        radius = max(4.0, min(24.0, width / 6.0, height / 3.0))
        painter.drawRoundedRect(
            QRectF(x + 0.5, y + 0.5, width - 1.0, height - 1.0),
            radius,
            radius,
        )
    painter.end()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not image.save(str(path), "PNG"):
        raise RuntimeError("无法创建多选框白色遮盖图。")
    return path


def build_cover_filter(options: ExportOptions) -> str:
    if options.cover_image_path is None:
        raise ValueError("白色遮盖图尚未创建。")
    value = "[0:v][1:v]overlay=x=0:y=0:shortest=1"
    if options.range_start is not None and options.range_end is not None:
        value += (
            f":enable='between(t,{options.range_start:.3f},{options.range_end:.3f})'"
        )
    return value + "[vout]"


def build_soft_blur_filter(options: ExportOptions) -> str:
    """生成带羽化遮罩的强高斯模糊，适合大面积隐私区域。"""
    enable = ""
    if options.range_start is not None and options.range_end is not None:
        enable = (
            f":enable='between(t,{options.range_start:.3f},{options.range_end:.3f})'"
        )
    drawboxes = []
    for selection in all_selections(options):
        x, y, width, height = adjust_rect_for_yuv420p(
            selection,
            options.video_width,
            options.video_height,
        )
        drawboxes.append(
            f"drawbox=x={x}:y={y}:w={width}:h={height}:"
            f"color=white:t=fill{enable}"
        )
    return (
        "[0:v]split=3[base][blur_src][mask_src];"
        "[blur_src]gblur=sigma=24:steps=2[blurred];"
        "[mask_src]format=gray,geq=lum=0,"
        + ",".join(drawboxes)
        + ","
        "gblur=sigma=5:steps=2[mask];"
        "[base][blurred][mask]maskedmerge[vout]"
    )


def should_copy_audio(options: ExportOptions) -> bool:
    return bool(
        options.audio_codec
        and not options.force_aac
        and options.audio_codec.lower() in MP4_COPY_AUDIO_CODECS
    )


def video_encoder_args(options: ExportOptions) -> list[str]:
    """Use Apple hardware encoding on macOS and libx264 elsewhere."""

    if sys.platform == "darwin":
        return [
            "-c:v",
            "h264_videotoolbox",
            "-q:v",
            "70",
            "-allow_sw",
            "1",
            "-pix_fmt",
            "yuv420p",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        options.preset,
        "-crf",
        str(options.crf),
        "-pix_fmt",
        "yuv420p",
    ]


def build_ffmpeg_args(options: ExportOptions) -> list[str]:
    mode = resolve_effect_mode(options)
    args = [
        "-hide_banner",
        "-y",
        "-i",
        str(options.input_path),
    ]
    if mode == "solid_cover":
        if options.cover_image_path is None:
            raise ValueError("白色遮盖图尚未创建。")
        args.extend(
            [
                "-loop",
                "1",
                "-framerate",
                "1",
                "-i",
                str(options.cover_image_path),
                "-filter_complex",
                build_cover_filter(options),
                "-map",
                "[vout]",
            ]
        )
    elif mode == "soft_blur":
        args.extend(
            [
                "-filter_complex",
                build_soft_blur_filter(options),
                "-map",
                "[vout]",
            ]
        )
    else:
        args.extend(["-map", "0:v:0", "-vf", build_delogo_filter(options)])
    args.extend(
        [
        "-map",
        "0:a?",
        ]
    )
    args.extend(video_encoder_args(options))
    if options.audio_codec:
        if should_copy_audio(options):
            args.extend(["-c:a", "copy"])
        else:
            args.extend(["-c:a", "aac", "-b:a", "192k"])
    else:
        args.append("-an")
    args.extend(
        [
            "-map_metadata",
            "0",
            "-movflags",
            "+faststart",
            "-progress",
            "pipe:1",
            "-nostats",
            str(options.output_path),
        ]
    )
    return args


def unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError("无法生成不重名的输出文件。")


def generate_preview_frame(
    ffmpeg: Path, options: ExportOptions, position: float, destination: Path
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    preview_options = replace(options, range_start=None, range_end=None)
    temporary_cover: Path | None = None
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0, position):.3f}",
        "-i",
        str(options.input_path),
    ]
    preview_mode = resolve_effect_mode(preview_options)
    if preview_mode == "solid_cover":
        temporary_cover = destination.with_name(
            f".preview_cover_{uuid.uuid4().hex}.png"
        )
        create_multi_cover_image(
            temporary_cover,
            preview_options.video_width,
            preview_options.video_height,
            all_selections(preview_options),
        )
        preview_options = replace(
            preview_options, cover_image_path=temporary_cover
        )
        command.extend(
            [
                "-loop",
                "1",
                "-framerate",
                "1",
                "-i",
                str(temporary_cover),
                "-filter_complex",
                build_cover_filter(preview_options),
                "-map",
                "[vout]",
            ]
        )
    elif preview_mode == "soft_blur":
        boxes = []
        for selection in all_selections(preview_options):
            x, y, width, height = adjust_rect_for_yuv420p(
                selection,
                preview_options.video_width,
                preview_options.video_height,
            )
            boxes.append(
                f"drawbox=x={x}:y={y}:w={width}:h={height}:"
                "color=lime@0.9:t=2"
            )
        preview_filter = (
            build_soft_blur_filter(preview_options)
            + ";[vout]"
            + ",".join(boxes)
            + "[preview]"
        )
        command.extend(["-filter_complex", preview_filter, "-map", "[preview]"])
    else:
        command.extend(
            ["-vf", build_delogo_filter(preview_options, show=True)]
        )
    command.extend(["-frames:v", "1", "-y", str(destination)])
    LOGGER.info("生成处理区域预览: %s", shlex.join(command))
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=60,
        )
    finally:
        if temporary_cover is not None:
            temporary_cover.unlink(missing_ok=True)
    if result.returncode != 0 or not destination.is_file():
        raise RuntimeError("无法生成预览图。" + (f"\n{result.stderr}" if result.stderr else ""))
    return destination


class FFmpegRunner(QObject):
    progressChanged = Signal(float, float, float)
    statusChanged = Signal(str)
    errorOccurred = Signal(str)
    finished = Signal(str)
    cancelled = Signal()

    def __init__(self, ffmpeg: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.ffmpeg = Path(ffmpeg)
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._on_finished)
        self.process.errorOccurred.connect(self._on_process_error)
        self._options: ExportOptions | None = None
        self._stdout_buffer = ""
        self._stderr_tail = ""
        self._cancel_requested = False
        self._retried_aac = False
        self._temporary_cover: Path | None = None

    @property
    def running(self) -> bool:
        return self.process.state() != QProcess.ProcessState.NotRunning

    def start(self, options: ExportOptions) -> None:
        if self.running:
            raise RuntimeError("已有处理任务正在运行。")
        validate_export(options, self.ffmpeg)
        if resolve_effect_mode(options) == "solid_cover":
            self._temporary_cover = options.output_path.parent / (
                f".videocleaner_cover_{uuid.uuid4().hex}.png"
            )
            create_multi_cover_image(
                self._temporary_cover,
                options.video_width,
                options.video_height,
                all_selections(options),
            )
            options = replace(options, cover_image_path=self._temporary_cover)
        else:
            self._temporary_cover = None
        self._options = options
        self._cancel_requested = False
        self._retried_aac = False
        self._launch()

    def _launch(self) -> None:
        assert self._options is not None
        self._stdout_buffer = ""
        self._stderr_tail = ""
        args = build_ffmpeg_args(self._options)
        LOGGER.info("FFmpeg 命令: %s", shlex.join([str(self.ffmpeg), *args]))
        self.statusChanged.emit("正在处理视频…")
        self.process.setProgram(str(self.ffmpeg))
        self.process.setArguments(args)
        self.process.start()

    def cancel(self) -> None:
        if not self.running:
            return
        self._cancel_requested = True
        self.statusChanged.emit("正在取消处理…")
        LOGGER.info("用户取消处理")
        self.process.terminate()
        QTimer.singleShot(2000, self._kill_if_needed)

    def _kill_if_needed(self) -> None:
        if self.running:
            self.process.kill()

    def _read_stdout(self) -> None:
        chunk = bytes(self.process.readAllStandardOutput()).decode("utf-8", "replace")
        self._stdout_buffer += chunk
        lines = self._stdout_buffer.splitlines(keepends=True)
        self._stdout_buffer = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._stdout_buffer = lines.pop()
        for raw in lines:
            line = raw.strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            seconds: float | None = None
            if key == "out_time_us":
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    pass
            elif key == "out_time":
                try:
                    hh, mm, ss = value.split(":")
                    seconds = int(hh) * 3600 + int(mm) * 60 + float(ss)
                except (ValueError, TypeError):
                    pass
            if seconds is not None and self._options is not None:
                total = max(self._options.duration, 0.001)
                percent = min(max(seconds / total * 100, 0), 100)
                self.progressChanged.emit(percent, seconds, total)

    def _read_stderr(self) -> None:
        chunk = bytes(self.process.readAllStandardError()).decode("utf-8", "replace")
        self._stderr_tail = (self._stderr_tail + chunk)[-12000:]

    def _on_process_error(self, _error: QProcess.ProcessError) -> None:
        if self.process.state() == QProcess.ProcessState.NotRunning and not self._cancel_requested:
            LOGGER.error("FFmpeg 无法启动: %s", self.process.errorString())

    def _remove_partial(self) -> None:
        if self._options is None:
            return
        try:
            if self._options.output_path.exists():
                self._options.output_path.unlink()
        except OSError:
            LOGGER.warning("未能删除未完成输出: %s", self._options.output_path)

    def _cleanup_temporary_cover(self) -> None:
        if self._temporary_cover is None:
            return
        try:
            self._temporary_cover.unlink(missing_ok=True)
        except OSError:
            LOGGER.warning("未能删除临时遮盖图: %s", self._temporary_cover)
        self._temporary_cover = None

    def _is_audio_copy_failure(self) -> bool:
        """只在封装器明确拒绝复制音频时改用 AAC，避免掩盖视频滤镜错误。"""
        detail = self._stderr_tail.lower()
        indicators = (
            "could not find tag for codec",
            "codec not currently supported in container",
            "codec not supported in container",
            "muxer does not support",
            "could not write header",
            "incorrect codec parameters",
        )
        return any(indicator in detail for indicator in indicators)

    def _on_finished(
        self, exit_code: int, exit_status: QProcess.ExitStatus
    ) -> None:
        self._read_stdout()
        self._read_stderr()
        if self._cancel_requested:
            self._remove_partial()
            self._cleanup_temporary_cover()
            self.cancelled.emit()
            return
        assert self._options is not None
        successful = (
            exit_status == QProcess.ExitStatus.NormalExit
            and exit_code == 0
            and self._options.output_path.is_file()
        )
        if successful:
            LOGGER.info("处理完成: %s", self._options.output_path)
            self._cleanup_temporary_cover()
            self.progressChanged.emit(100.0, self._options.duration, self._options.duration)
            self.finished.emit(str(self._options.output_path))
            return
        if (
            should_copy_audio(self._options)
            and not self._retried_aac
            and self._is_audio_copy_failure()
        ):
            LOGGER.warning("音频复制处理失败，改用 AAC 重试: %s", self._stderr_tail)
            self._remove_partial()
            self._retried_aac = True
            self._options = replace(self._options, force_aac=True)
            self.statusChanged.emit("音频复制失败，正在自动改用 AAC…")
            QTimer.singleShot(0, self._launch)
            return
        self._remove_partial()
        self._cleanup_temporary_cover()
        detail = self._stderr_tail.strip() or self.process.errorString()
        LOGGER.error("FFmpeg 处理失败: %s", detail)
        if "logo area is outside of the frame" in detail.lower():
            message = "处理区域过于贴近视频边缘，请稍微缩小选框后重试。"
        elif "no space left on device" in detail.lower():
            message = "磁盘剩余空间不足，请清理输出磁盘空间后重试。"
        elif "permission denied" in detail.lower():
            message = "无法写入输出位置，请选择其他输出目录。"
        else:
            message = "视频处理失败，请查看日志了解详细原因。"
        self.errorOccurred.emit(message)
