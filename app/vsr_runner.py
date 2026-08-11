"""本地 STTN 时序修复引擎的异步运行与输出规范化。"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from .coordinate_mapper import adjust_rect_for_yuv420p
from .ai_pipeline_policy import (
    worker_time_range_args,
    worker_video_is_final_encode,
)
from .ffmpeg_runner import (
    ExportOptions,
    all_selections,
    should_copy_audio,
    validate_export,
    video_encoder_args,
)
from .logger import LOGGER
from .vsr_locator import VSRPaths, require_vsr


AI_EFFECT_MODES = {
    "ai_propainter_fast",
    "ai_propainter",
    "ai_strong",
    "ai_fast",
    "ai_precise",
    "ai_full",
}


def build_vsr_worker_command(
    paths: VSRPaths,
    options: ExportOptions,
    intermediate: Path,
    ffmpeg: Path | None = None,
) -> list[str]:
    x, y, width, height = adjust_rect_for_yuv420p(
        options.selection,
        options.video_width,
        options.video_height,
    )
    if (
        sys.platform == "darwin"
        and getattr(sys, "frozen", False)
        and paths.python.resolve() == Path(sys.executable).resolve()
    ):
        prefix = [str(paths.python), "--vsr-worker"]
    else:
        prefix = [str(paths.python), "-u", str(paths.bridge_worker)]
    command = [
        *prefix,
        "--engine-root",
        str(paths.engine_root),
        "--input",
        str(options.input_path),
        "--output",
        str(intermediate),
        "--coords",
        str(y),
        str(y + height),
        str(x),
        str(x + width),
        "--mode",
        {
            "ai_propainter_fast": "propainter_fast",
            "ai_propainter": "propainter",
            "ai_strong": "strong",
            "ai_fast": "fast",
            "ai_precise": "precise",
            "ai_full": "full",
        }[options.effect_mode],
    ]
    if ffmpeg is not None:
        command.extend(["--ffmpeg", str(ffmpeg)])
    command.extend(worker_time_range_args(options.range_start, options.range_end))
    return command


def build_ai_finalize_command(
    ffmpeg: Path,
    options: ExportOptions,
    repaired_video: Path,
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
        "-i",
        str(repaired_video),
    ]
    strong_single_encode = worker_video_is_final_encode(options.effect_mode)
    if (
        not strong_single_encode
        and options.range_start is not None
        and options.range_end is not None
    ):
        command.extend(
            [
                "-filter_complex",
                (
                    "[0:v][1:v]blend="
                    "all_expr='if(between(T,"
                    f"{options.range_start:.3f},{options.range_end:.3f}"
                    "),B,A)'[vout]"
                ),
                "-map",
                "[vout]",
            ]
        )
    else:
        command.extend(["-map", "1:v:0"])
    command.extend(
        [
            "-map",
            "0:a?",
        ]
    )
    if strong_single_encode:
        # The M2 Pro strong worker already writes H.264 with VideoToolbox.
        # Finalization only restores the source audio and metadata, avoiding
        # another full decode/encode pass.
        command.extend(["-c:v", "copy"])
    else:
        command.extend(video_encoder_args(options))
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


class VSRRunner(QThread):
    """在独立线程和独立 Python 进程中执行本地 AI 修复。"""

    progressChanged = Signal(float, float, float)
    statusChanged = Signal(str)
    errorOccurred = Signal(str)
    completed = Signal(str)
    cancelled = Signal()

    def __init__(self, ffmpeg: Path, parent=None) -> None:
        super().__init__(parent)
        self.ffmpeg = Path(ffmpeg)
        self._options: ExportOptions | None = None
        self._paths: VSRPaths | None = None
        self._cancel_event = threading.Event()
        self._process_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    @property
    def running(self) -> bool:
        return self.isRunning()

    def start_with(self, options: ExportOptions) -> None:
        if self.isRunning():
            raise RuntimeError("已有本地 AI 处理任务正在运行。")
        if options.effect_mode not in AI_EFFECT_MODES:
            raise ValueError("本地 AI 处理模式无效。")
        validate_export(options, self.ffmpeg)
        self._paths = require_vsr()
        self._options = options
        self._cancel_event.clear()
        self.start()

    def _set_process(self, process: subprocess.Popen[str] | None) -> None:
        with self._process_lock:
            self._process = process

    def _terminate_process_tree(self) -> None:
        with self._process_lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        LOGGER.info("正在终止本地 AI 子进程树，PID=%s", process.pid)
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def cancel(self) -> None:
        if not self.isRunning():
            return
        LOGGER.info("用户取消本地 AI 时序修复")
        self.statusChanged.emit("正在取消本地 AI 处理…")
        self._cancel_event.set()
        self._terminate_process_tree()

    def _run_worker(
        self,
        paths: VSRPaths,
        options: ExportOptions,
        intermediate: Path,
        error_lines: list[str],
        selection_index: int = 0,
        selection_count: int = 1,
    ) -> None:
        command = build_vsr_worker_command(
            paths,
            options,
            intermediate,
            ffmpeg=self.ffmpeg,
        )
        LOGGER.info("本地 AI 引擎命令: %s", subprocess.list2cmdline(command))
        environment = os.environ.copy()
        python_root = paths.python.parent
        site_packages = python_root / "Lib" / "site-packages"
        runtime_temp = intermediate.parent / "runtime_temp"
        runtime_temp.mkdir(parents=True, exist_ok=True)
        dll_paths = [
            site_packages / "torch" / "lib",
            site_packages / "torch_directml",
            python_root,
        ]
        common_environment = {
                # QPT 打包环境中只要该值包含 "ing" 就不会再次执行安装器。
                "QPT_MODE": "Running",
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
                "OMP_NUM_THREADS": str(max(2, min(6, os.cpu_count() or 4))),
                "TEMP": str(runtime_temp),
                "TMP": str(runtime_temp),
                "PIP_CACHE_DIR": str(runtime_temp / "pip_cache"),
                "PADDLE_HOME": str(runtime_temp / "paddle"),
                "TORCH_HOME": str(runtime_temp / "torch"),
                "XDG_CACHE_HOME": str(runtime_temp / "cache"),
            }
        environment.update(common_environment)
        if os.name == "nt":
            environment.update(
                {
                    "PYTHONPATH": os.pathsep.join(
                        [
                            str(site_packages),
                            str(python_root / "Lib"),
                            str(python_root),
                        ]
                    ),
                    "PATH": os.pathsep.join(
                        [
                            *(str(path) for path in dll_paths),
                            environment.get("PATH", ""),
                        ]
                    ),
                }
            )
        else:
            environment.update(
                {
                    "PYTORCH_ENABLE_MPS_FALLBACK": "1",
                }
            )
        process = subprocess.Popen(
            command,
            cwd=paths.engine_root,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            ),
            start_new_session=(os.name != "nt"),
        )
        self._set_process(process)
        assert process.stdout is not None
        for raw_line in process.stdout:
            if self._cancel_event.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue
            LOGGER.info("VSR: %s", line)
            error_lines.append(line)
            del error_lines[:-120]
            if line.startswith("VIDEOCLEANER_PROGRESS="):
                try:
                    engine_percent = float(line.split("=", 1)[1])
                except ValueError:
                    continue
                stage_progress = max(0.0, min(1.0, engine_percent / 100.0))
                percent = (
                    (selection_index + stage_progress)
                    / max(selection_count, 1)
                    * 90.0
                )
                self.progressChanged.emit(
                    percent,
                    options.duration * percent / 100.0,
                    options.duration,
                )
            elif line.startswith("VIDEOCLEANER_STATUS="):
                status = line.split("=", 1)[1]
                if selection_count > 1:
                    status = (
                        f"正在处理第 {selection_index + 1}/{selection_count} "
                        f"个选框：{status}"
                    )
                self.statusChanged.emit(status)
        if self._cancel_event.is_set():
            self._terminate_process_tree()
            raise InterruptedError
        exit_code = process.wait()
        self._set_process(None)
        if exit_code != 0 or not intermediate.is_file():
            detail = "\n".join(error_lines[-25:])
            raise RuntimeError(detail or f"本地 AI 引擎退出代码：{exit_code}")

    def _run_finalize(
        self,
        options: ExportOptions,
        intermediate: Path,
        copy_audio: bool,
        error_lines: list[str],
    ) -> int:
        command = build_ai_finalize_command(
            self.ffmpeg,
            options,
            intermediate,
            copy_audio=copy_audio,
        )
        LOGGER.info("本地 AI 输出封装命令: %s", subprocess.list2cmdline(command))
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._set_process(process)
        assert process.stdout is not None
        for raw_line in process.stdout:
            if self._cancel_event.is_set():
                break
            key, separator, value = raw_line.strip().partition("=")
            if separator and key in {"out_time_us", "out_time_ms"}:
                try:
                    microseconds = int(value)
                except ValueError:
                    continue
                seconds = max(0.0, microseconds / 1_000_000)
                percent = 90.0 + min(
                    10.0,
                    seconds / max(options.duration, 0.001) * 10.0,
                )
                self.progressChanged.emit(percent, seconds, options.duration)
        if self._cancel_event.is_set():
            self._terminate_process_tree()
            raise InterruptedError
        stdout_tail, stderr_text = process.communicate()
        self._set_process(None)
        if stdout_tail:
            error_lines.extend(stdout_tail.splitlines())
        if stderr_text:
            error_lines.extend(stderr_text.splitlines())
        return process.returncode

    def run(self) -> None:
        options = self._options
        paths = self._paths
        if options is None or paths is None:
            return
        work_directory: Path | None = None
        error_lines: list[str] = []
        try:
            options.output_path.parent.mkdir(parents=True, exist_ok=True)
            work_directory = Path(
                tempfile.mkdtemp(
                    prefix=".videocleaner_ai_",
                    dir=options.output_path.parent,
                )
            )
            selections = all_selections(options)
            current_input = options.input_path
            intermediate = work_directory / "repaired_engine_0.mp4"
            for index, selection in enumerate(selections):
                intermediate = work_directory / f"repaired_engine_{index + 1}.mp4"
                stage_options = replace(
                    options,
                    input_path=current_input,
                    selection=selection,
                    selections=(),
                )
                self.statusChanged.emit(
                    f"正在启动本地 AI 修复第 {index + 1}/{len(selections)} 个选框…"
                )
                self._run_worker(
                    paths,
                    stage_options,
                    intermediate,
                    error_lines,
                    selection_index=index,
                    selection_count=len(selections),
                )
                current_input = intermediate

            if self._cancel_event.is_set():
                raise InterruptedError
            self.statusChanged.emit("正在生成兼容的 H.264 MP4 并恢复原音频…")
            copy_audio = should_copy_audio(options)
            exit_code = self._run_finalize(
                options,
                intermediate,
                copy_audio=copy_audio,
                error_lines=error_lines,
            )
            if exit_code != 0 and copy_audio and not self._cancel_event.is_set():
                LOGGER.warning("音频复制失败，本地 AI 输出改用 AAC")
                options.output_path.unlink(missing_ok=True)
                error_lines.clear()
                exit_code = self._run_finalize(
                    options,
                    intermediate,
                    copy_audio=False,
                    error_lines=error_lines,
                )
            if exit_code != 0 or not options.output_path.is_file():
                raise RuntimeError(
                    "\n".join(error_lines[-30:]) or "生成最终 MP4 文件失败。"
                )
            LOGGER.info("本地 AI 时序修复完成: %s", options.output_path)
            self.progressChanged.emit(100.0, options.duration, options.duration)
            self.completed.emit(str(options.output_path))
        except InterruptedError:
            options.output_path.unlink(missing_ok=True)
            self.cancelled.emit()
        except Exception:
            LOGGER.exception("本地 AI 时序修复失败")
            options.output_path.unlink(missing_ok=True)
            if self._cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.errorOccurred.emit(
                    "本地 AI 去字失败。详细原因已经写入日志；"
                    "可改用“快速笔画修补”继续处理。"
                )
        finally:
            self._terminate_process_tree()
            self._set_process(None)
            if work_directory is not None:
                shutil.rmtree(work_directory, ignore_errors=True)
