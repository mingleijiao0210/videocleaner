"""应用常量和路径。"""

from __future__ import annotations

import sys
import os
from pathlib import Path

APP_NAME = "视频文字区域去除工具"
APP_VERSION = "1.8.1"
SUPPORTED_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}
VIDEO_FILTER = "视频文件 (*.mp4 *.mov *.avi *.mkv *.m4v *.webm)"
DEFAULT_CRF = 18
DEFAULT_PRESET = "medium"
MIN_SELECTION_SIZE = 4


def project_root() -> Path:
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            executable = Path(sys.executable).resolve()
            resources = executable.parent.parent / "Resources"
            if resources.is_dir():
                return resources
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def writable_root() -> Path:
    """日志和默认输出使用的可写目录。"""
    configured = os.environ.get("VIDEOCLEANER_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "VideoCleaner"
        )
    return project_root()
