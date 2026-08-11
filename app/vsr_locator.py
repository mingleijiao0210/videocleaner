"""定位随软件携带的本地视频时序修复引擎。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
import os

from .settings import project_root


@dataclass(frozen=True)
class VSRPaths:
    engine_root: Path
    python: Path
    backend_main: Path
    bridge_worker: Path


def _engine_candidates(base: Path) -> list[Path]:
    tools_root = base / "tools" / "vsr"
    candidates = [
        tools_root / "engine",
        tools_root / "engine" / "video-subtitle-remover",
        tools_root / "engine" / "video-subtitle-remover-main",
        tools_root / "engine" / "vsr",
    ]
    configured = os.environ.get("VIDEOCLEANER_VSR_ROOT")
    if configured:
        candidates.insert(0, Path(configured).expanduser())
    return candidates


def _find_python(engine_root: Path, project: Path | None = None) -> Path | None:
    candidates = [
        engine_root / "python.exe",
        engine_root / "Python" / "python.exe",
        engine_root / "python" / "python.exe",
        engine_root / "runtime" / "python.exe",
        engine_root / "videoEnv" / "python.exe",
        engine_root / "venv" / "Scripts" / "python.exe",
        engine_root / ".venv" / "bin" / "python3",
        engine_root / ".venv" / "bin" / "python",
        engine_root / "venv" / "bin" / "python3",
        engine_root / "venv" / "bin" / "python",
    ]
    if project is not None:
        candidates.extend(
            [
                project / ".venv-macos" / "bin" / "python3",
                project / ".venv-macos" / "bin" / "python",
                project / ".venv" / "bin" / "python3",
                project / ".venv" / "bin" / "python",
            ]
        )
    if sys.platform == "darwin" and getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).resolve())
    return next((path for path in candidates if path.is_file()), None)


def locate_vsr(base: Path | None = None) -> VSRPaths | None:
    """按软件目录优先的固定位置查找离线引擎，不扫描用户目录。"""

    root = Path(base) if base is not None else project_root()
    bridge = root / "tools" / "vsr" / "bridge" / "videocleaner_vsr_worker.py"
    if not bridge.is_file():
        return None
    for candidate in _engine_candidates(root):
        python = _find_python(candidate, root)
        source_roots = [candidate, candidate / "resources"]
        source_root = next(
            (
                value
                for value in source_roots
                if (value / "backend" / "main.py").is_file()
            ),
            None,
        )
        if source_root is not None and python is not None:
            return VSRPaths(
                engine_root=source_root,
                python=python,
                backend_main=source_root / "backend" / "main.py",
                bridge_worker=bridge,
            )
    return None


def require_vsr(base: Path | None = None) -> VSRPaths:
    paths = locate_vsr(base)
    if paths is None:
        root = Path(base) if base is not None else project_root()
        expected = root / "tools" / "vsr" / "engine"
        raise FileNotFoundError(
            "本地 AI 时序修复引擎尚未安装完整。\n"
            f"应位于：{expected}\n"
            "可以暂时选择“快速笔画修补”或“插值去除”。"
        )
    return paths
