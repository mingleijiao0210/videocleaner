from pathlib import Path

import pytest

from app.enhancement_runner import (
    EnhancementOptions,
    build_ai_command,
    build_clarity_command,
    validate_enhancement,
)


def options(tmp_path: Path, mode: str = "clarity") -> EnhancementOptions:
    source = tmp_path / "input.mp4"
    source.write_bytes(b"video")
    return EnhancementOptions(
        input_path=source,
        output_path=tmp_path / "output.mp4",
        mode=mode,
        duration=2.0,
        width=640,
        height=360,
        audio_codec="aac",
    )


def test_clarity_uses_conservative_denoise_and_sharpen(tmp_path, monkeypatch):
    monkeypatch.setattr("app.enhancement_runner.sys.platform", "darwin")
    command = build_clarity_command(
        tmp_path / "ffmpeg",
        options(tmp_path),
        copy_audio=True,
    )

    assert "hqdn3d=1.0:1.0:4.0:4.0,unsharp=5:5:0.55:3:3:0.0" in command
    assert "h264_videotoolbox" in command
    assert command[command.index("-c:a") + 1] == "copy"


def test_ai_2x_rejects_more_than_4k(tmp_path):
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_bytes(b"binary")
    too_large = EnhancementOptions(
        input_path=options(tmp_path).input_path,
        output_path=tmp_path / "large.mp4",
        mode="ai_2x",
        duration=2.0,
        width=2560,
        height=1440,
        audio_codec=None,
    )

    with pytest.raises(ValueError, match="4K"):
        validate_enhancement(too_large, ffmpeg)


def test_ai_command_uses_bundled_worker_and_model(tmp_path, monkeypatch):
    project = tmp_path / "project"
    worker = (
        project
        / "tools"
        / "super_resolution"
        / "videocleaner_superres_worker.py"
    )
    model = (
        project
        / "tools"
        / "super_resolution"
        / "models"
        / "FSRCNN_x2.pb"
    )
    worker.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    worker.write_text("pass", encoding="utf-8")
    model.write_bytes(b"model")

    class Paths:
        python = tmp_path / "python3"

    monkeypatch.setattr("app.enhancement_runner.project_root", lambda: project)
    monkeypatch.setattr("app.enhancement_runner.require_vsr", lambda: Paths())
    command = build_ai_command(tmp_path / "ffmpeg", options(tmp_path, "ai_2x"))

    assert str(worker) in command
    assert str(model) in command
    assert command[0] == str(Paths.python)
