from pathlib import Path

import pytest

from app.vsr_locator import locate_vsr, require_vsr


def _make_engine(root: Path, python_relative: str = "python.exe") -> None:
    bridge = root / "tools" / "vsr" / "bridge" / "videocleaner_vsr_worker.py"
    backend = root / "tools" / "vsr" / "engine" / "backend" / "main.py"
    python = root / "tools" / "vsr" / "engine" / python_relative
    for path in (bridge, backend, python):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test", encoding="utf-8")


def test_locate_vsr_from_expected_layout(tmp_path):
    _make_engine(tmp_path)
    paths = locate_vsr(tmp_path)
    assert paths is not None
    assert paths.python == tmp_path / "tools" / "vsr" / "engine" / "python.exe"
    assert paths.backend_main.is_file()
    assert paths.bridge_worker.is_file()


def test_locate_vsr_supports_nested_release_folder(tmp_path):
    bridge = tmp_path / "tools" / "vsr" / "bridge" / "videocleaner_vsr_worker.py"
    engine = (
        tmp_path
        / "tools"
        / "vsr"
        / "engine"
        / "video-subtitle-remover-main"
    )
    for path in (bridge, engine / "backend" / "main.py", engine / "python.exe"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test", encoding="utf-8")
    paths = locate_vsr(tmp_path)
    assert paths is not None
    assert paths.engine_root == engine


def test_locate_qpt_release_layout(tmp_path):
    bridge = tmp_path / "tools" / "vsr" / "bridge" / "videocleaner_vsr_worker.py"
    package = tmp_path / "tools" / "vsr" / "engine"
    for path in (
        bridge,
        package / "resources" / "backend" / "main.py",
        package / "Python" / "python.exe",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# test", encoding="utf-8")
    paths = locate_vsr(tmp_path)
    assert paths is not None
    assert paths.engine_root == package / "resources"
    assert paths.python == package / "Python" / "python.exe"


def test_require_vsr_gives_plain_chinese_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="本地 AI 时序修复引擎"):
        require_vsr(tmp_path)
