"""FFmpeg 和 ffprobe 路径检测。"""

from __future__ import annotations

import shutil
import sys
import os
from dataclasses import dataclass
from pathlib import Path

from .settings import project_root


@dataclass(frozen=True)
class FFmpegPaths:
    ffmpeg: Path
    ffprobe: Path


def _candidate_bins() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("VIDEOCLEANER_FFMPEG_DIR")
    if configured:
        candidates.append(Path(configured).expanduser())
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                exe_dir / "tools" / "ffmpeg" / "bin",
                exe_dir / "_internal" / "tools" / "ffmpeg" / "bin",
                Path(getattr(sys, "_MEIPASS", exe_dir))
                / "tools"
                / "ffmpeg"
                / "bin",
            ]
        )
    root = project_root()
    candidates.extend([root / "tools" / "ffmpeg" / "bin", root / "ffmpeg" / "bin"])
    if sys.platform == "darwin":
        candidates.extend(
            [
                Path("/opt/homebrew/bin"),
                Path("/usr/local/bin"),
            ]
        )
    return candidates


def locate_ffmpeg() -> FFmpegPaths | None:
    ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    for bin_dir in _candidate_bins():
        ffmpeg = bin_dir / ffmpeg_name
        ffprobe = bin_dir / ffprobe_name
        if ffmpeg.is_file() and ffprobe.is_file():
            return FFmpegPaths(ffmpeg.resolve(), ffprobe.resolve())
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path and ffprobe_path:
        return FFmpegPaths(Path(ffmpeg_path).resolve(), Path(ffprobe_path).resolve())
    return None


def require_ffmpeg() -> FFmpegPaths:
    paths = locate_ffmpeg()
    if paths is None:
        raise FileNotFoundError(
            "没有找到 FFmpeg。请确认程序目录的 tools/ffmpeg/bin 中包含 "
            "ffmpeg 和 ffprobe（Windows 文件名带 .exe）。"
        )
    return paths
