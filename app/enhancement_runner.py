"""Asynchronous whole-frame clarity and local AI super-resolution."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .ffmpeg_runner import MP4_COPY_AUDIO_CODECS
from .logger import LOGGER
from .settings import project_root
from .vsr_locator import require_vsr

ENHANCEMENT_MODES = {"clarity", "ai_2x"}


@dataclass(frozen=True)
class EnhancementOptions:
    input_path: Path
    output_path: Path
    mode: str
    duration: float
    width: int
    height: int
    audio_codec: str | None


def _video_encoder_args() -> list[str]:
    if sys.platform == "darwin":
        return [
            "-c:v",
            "h264_videotoolbox",
            "-q:v",
            "70",
            "-pix_fmt",
            "yuv420p",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
    ]


def validate_enhancement(options: EnhancementOptions, ffmpeg: Path) -> None:
    if options.mode not in ENHANCEMENT_MODES:
        raise ValueError("未知的画面增强模式。")
    if not Path(ffmpeg).is_file():
        raise FileNotFoundError("FFmpeg 不存在。")
    if not options.input_path.is_file():
        raise FileNotFoundError("输入视频不存在。")
    if options.input_path.resolve() == options.output_path.resolve():
        raise ValueError("输出文件不能覆盖原视频。")
    if options.width <= 0 or options.height <= 0 or options.duration <= 0:
        raise ValueError("输入视频参数无效。")
    if options.mode == "ai_2x" and (
        options.width * 2 > 3840
        or options.height * 2 > 3840
        or options.width * options.height * 4 > 3840 * 2160
    ):
        raise ValueError("AI 2× 超清的输出不能超过 4K，请改用快速清晰增强。")
    options.output_path.parent.mkdir(parents=True, exist_ok=True)


def build_clarity_command(
    ffmpeg: Path,
    options: EnhancementOptions,
    copy_audio: bool,
) -> list[str]:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(options.input_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        "hqdn3d=1.0:1.0:4.0:4.0,unsharp=5:5:0.55:3:3:0.0",
        *_video_encoder_args(),
    ]
    if options.audio_codec:
        if copy_audio:
            command.extend(["-c:a", "copy"])
        else:
            command.extend(["-c:a", "aac", "-b:a", "192k"])
    else:
        command.append("-an")
    command.extend(
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
    return command


def build_ai_command(
    ffmpeg: Path,
    options: EnhancementOptions,
) -> list[str]:
    paths = require_vsr()
    worker = (
        project_root()
        / "tools"
        / "super_resolution"
        / "videocleaner_superres_worker.py"
    )
    model = (
        project_root()
        / "tools"
        / "super_resolution"
        / "models"
        / "FSRCNN_x2.pb"
    )
    if not worker.is_file() or not model.is_file():
        raise FileNotFoundError("AI 超清组件不完整，请重新安装完整软件包。")
    return [
        str(paths.python),
        "-u",
        str(worker),
        "--input",
        str(options.input_path),
        "--output",
        str(options.output_path),
        "--model",
        str(model),
        "--ffmpeg",
        str(ffmpeg),
    ]


class EnhancementRunner(QThread):
    progressChanged = Signal(float, float, float)
    statusChanged = Signal(str)
    errorOccurred = Signal(str)
    completed = Signal(str)
    cancelled = Signal()

    def __init__(self, ffmpeg: Path, parent=None) -> None:
        super().__init__(parent)
        self.ffmpeg = Path(ffmpeg)
        self._options: EnhancementOptions | None = None
        self._cancel_event = threading.Event()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.isRunning()

    def start_with(self, options: EnhancementOptions) -> None:
        if self.isRunning():
            raise RuntimeError("已有画面增强任务正在运行。")
        validate_enhancement(options, self.ffmpeg)
        self._options = options
        self._cancel_event.clear()
        self.start()

    def _set_process(self, process: subprocess.Popen | None) -> None:
        with self._process_lock:
            self._process = process

    def _terminate_process_tree(self) -> None:
        with self._process_lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()

    def cancel(self) -> None:
        if not self.isRunning():
            return
        self.statusChanged.emit("正在取消画面增强…")
        self._cancel_event.set()
        self._terminate_process_tree()

    def _run_command(
        self,
        command: list[str],
        options: EnhancementOptions,
        ai_worker: bool,
    ) -> tuple[int, list[str]]:
        LOGGER.info("画面增强命令: %s", subprocess.list2cmdline(command))
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=(os.name != "nt"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._set_process(process)
        lines: list[str] = []
        assert process.stdout is not None
        for raw_line in process.stdout:
            if self._cancel_event.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue
            lines.append(line)
            del lines[:-100]
            if ai_worker and line.startswith("VIDEOCLEANER_PROGRESS="):
                try:
                    percent = float(line.split("=", 1)[1])
                except ValueError:
                    continue
                self.progressChanged.emit(
                    percent,
                    options.duration * percent / 100.0,
                    options.duration,
                )
            elif ai_worker and line.startswith("VIDEOCLEANER_STATUS="):
                self.statusChanged.emit(line.split("=", 1)[1])
            elif not ai_worker:
                key, separator, value = line.partition("=")
                if separator and key in {"out_time_us", "out_time_ms"}:
                    try:
                        seconds = max(0.0, int(value) / 1_000_000)
                    except ValueError:
                        continue
                    percent = min(100.0, seconds / options.duration * 100.0)
                    self.progressChanged.emit(percent, seconds, options.duration)
        if self._cancel_event.is_set():
            self._terminate_process_tree()
            raise InterruptedError
        exit_code = process.wait()
        self._set_process(None)
        return exit_code, lines

    def run(self) -> None:
        options = self._options
        if options is None:
            return
        try:
            if options.mode == "clarity":
                self.statusChanged.emit("正在降噪、恢复细节并使用硬件编码…")
                copy_audio = bool(
                    options.audio_codec
                    and options.audio_codec.lower() in MP4_COPY_AUDIO_CODECS
                )
                command = build_clarity_command(self.ffmpeg, options, copy_audio)
                exit_code, lines = self._run_command(command, options, False)
                if exit_code != 0 and copy_audio and not self._cancel_event.is_set():
                    options.output_path.unlink(missing_ok=True)
                    command = build_clarity_command(self.ffmpeg, options, False)
                    exit_code, lines = self._run_command(command, options, False)
            else:
                self.statusChanged.emit("正在启动本地 AI 2× 超清（最大输出 4K）…")
                command = build_ai_command(self.ffmpeg, options)
                exit_code, lines = self._run_command(command, options, True)
            if exit_code != 0 or not options.output_path.is_file():
                raise RuntimeError(
                    "\n".join(lines[-20:]) or "画面增强没有生成输出文件。"
                )
            self.progressChanged.emit(100.0, options.duration, options.duration)
            self.completed.emit(str(options.output_path))
        except InterruptedError:
            options.output_path.unlink(missing_ok=True)
            self.cancelled.emit()
        except Exception:
            LOGGER.exception("画面增强失败")
            options.output_path.unlink(missing_ok=True)
            self.errorOccurred.emit(
                "画面增强失败，详细原因已写入日志。"
                "可改用“快速清晰增强”或关闭超清后重试。"
            )
        finally:
            self._terminate_process_tree()
            self._set_process(None)
