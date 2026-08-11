from pathlib import Path

import pytest

from app.coordinate_mapper import Rect
from app.ffmpeg_runner import (
    ExportOptions,
    build_delogo_filter,
    build_cover_filter,
    build_ffmpeg_args,
    build_soft_blur_filter,
    create_multi_cover_image,
    create_rounded_cover_image,
    resolve_effect_mode,
    unique_output_path,
    validate_export,
    video_encoder_args,
)


def options(tmp_path: Path, **changes) -> ExportOptions:
    source = tmp_path / "输入 视频(测试).mp4"
    source.write_bytes(b"test")
    data = dict(
        input_path=source,
        output_path=tmp_path / "输出 文件.mp4",
        selection=Rect(101, 51, 300, 80),
        video_width=1280,
        video_height=720,
        duration=10.0,
        audio_codec="aac",
    )
    data.update(changes)
    return ExportOptions(**data)


def test_non_macos_command_uses_h264_compatibility_and_audio_copy(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("app.ffmpeg_runner.sys.platform", "win32")
    args = build_ffmpeg_args(options(tmp_path))
    joined = " ".join(args)
    assert "libx264" in args
    assert "yuv420p" in args
    assert "+faststart" in args
    assert args[args.index("-c:a") + 1] == "copy"
    assert str(tmp_path / "输入 视频(测试).mp4") in args
    assert "delogo=x=100:y=50:w=302:h=82:show=0" in joined


def test_macos_uses_videotoolbox_hardware_encoder(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ffmpeg_runner.sys.platform", "darwin")
    args = video_encoder_args(options(tmp_path))
    assert "h264_videotoolbox" in args
    assert "libx264" not in args
    assert args[args.index("-q:v") + 1] == "70"


def test_multiple_selections_are_processed_in_one_filter_chain(tmp_path):
    opt = options(
        tmp_path,
        selections=(
            Rect(101, 51, 300, 80),
            Rect(701, 401, 180, 60),
        ),
    )
    args = build_ffmpeg_args(opt)
    value = args[args.index("-vf") + 1]
    assert value.count("delogo=") == 2
    assert "delogo=x=100:y=50:w=302:h=82:show=0" in value
    assert "delogo=x=700:y=400:w=182:h=62:show=0" in value


def test_multiple_soft_blur_masks_include_every_selection(tmp_path):
    opt = options(
        tmp_path,
        effect_mode="soft_blur",
        selections=(
            Rect(101, 51, 300, 80),
            Rect(701, 401, 180, 60),
        ),
    )
    value = build_soft_blur_filter(opt)
    assert value.count("drawbox=") == 2
    assert "drawbox=x=100:y=50:w=302:h=82" in value
    assert "drawbox=x=700:y=400:w=182:h=62" in value


def test_range_uses_enable_without_trimming(tmp_path):
    args = build_ffmpeg_args(
        options(tmp_path, range_start=2.0, range_end=5.5)
    )
    filter_value = args[args.index("-vf") + 1]
    assert "enable='between(t,2.000,5.500)'" in filter_value
    assert "-ss" not in args
    assert "-t" not in args


def test_delogo_filter_insets_full_frame_edges(tmp_path):
    opt = options(
        tmp_path,
        selection=Rect(0, 26, 544, 198),
        video_width=544,
        video_height=960,
    )
    assert build_delogo_filter(opt) == "delogo=x=2:y=26:w=540:h=198:show=0"


def test_auto_large_selection_uses_soft_blur(tmp_path):
    opt = options(
        tmp_path,
        selection=Rect(0, 26, 544, 198),
        video_width=544,
        video_height=960,
    )
    assert resolve_effect_mode(opt) == "soft_blur"
    args = build_ffmpeg_args(opt)
    assert "-filter_complex" in args
    assert "-vf" not in args
    assert "[vout]" in args
    complex_filter = args[args.index("-filter_complex") + 1]
    assert "gblur=sigma=24" in complex_filter
    assert "drawbox=x=2:y=26:w=540:h=198" in complex_filter
    assert "format=gray,geq=lum=0" in complex_filter
    assert "maskedmerge[vout]" in complex_filter


def test_rounded_cover_png_is_created(tmp_path):
    path = create_rounded_cover_image(tmp_path / "rounded.png", 300, 80)
    assert path.is_file()
    assert path.stat().st_size > 100


def test_multi_cover_png_uses_full_video_size(tmp_path):
    path = create_multi_cover_image(
        tmp_path / "multi.png",
        1280,
        720,
        (Rect(100, 50, 300, 80), Rect(700, 400, 180, 60)),
    )
    from PySide6.QtGui import QImage

    image = QImage(str(path))
    assert image.width() == 1280
    assert image.height() == 720
    assert image.pixelColor(200, 80).alpha() == 255
    assert image.pixelColor(750, 430).alpha() == 255
    assert image.pixelColor(10, 10).alpha() == 0


def test_forced_delogo_keeps_interpolation_mode(tmp_path):
    opt = options(
        tmp_path,
        selection=Rect(0, 26, 544, 198),
        video_width=544,
        video_height=960,
        effect_mode="delogo",
    )
    args = build_ffmpeg_args(opt)
    assert "-vf" in args
    assert "-filter_complex" not in args


def test_soft_blur_range_only_enables_mask_during_range(tmp_path):
    opt = options(
        tmp_path,
        effect_mode="soft_blur",
        range_start=2.0,
        range_end=5.5,
    )
    value = build_soft_blur_filter(opt)
    assert "enable='between(t,2.000,5.500)'" in value


def test_solid_cover_range_uses_overlay_timeline(tmp_path):
    cover = tmp_path / "cover.png"
    create_rounded_cover_image(cover, 300, 80)
    opt = options(
        tmp_path,
        effect_mode="solid_cover",
        range_start=2.0,
        range_end=5.5,
        cover_image_path=cover,
    )
    assert "enable='between(t,2.000,5.500)'" in build_cover_filter(opt)


def test_incompatible_audio_uses_aac(tmp_path):
    args = build_ffmpeg_args(options(tmp_path, audio_codec="opus"))
    assert args[args.index("-c:a") + 1] == "aac"


def test_no_audio_does_not_create_audio(tmp_path):
    args = build_ffmpeg_args(options(tmp_path, audio_codec=None))
    assert "-an" in args


def test_validation_rejects_out_of_bounds(tmp_path):
    opt = options(tmp_path, selection=Rect(1200, 600, 200, 200))
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"x")
    with pytest.raises(ValueError, match="超出"):
        validate_export(opt, ffmpeg)


def test_validation_reports_invalid_second_selection(tmp_path):
    opt = options(
        tmp_path,
        selections=(
            Rect(100, 50, 300, 80),
            Rect(1200, 600, 200, 200),
        ),
    )
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"x")
    with pytest.raises(ValueError, match="第 2 个选框.*超出"):
        validate_export(opt, ffmpeg)


def test_validation_rejects_bad_range(tmp_path):
    opt = options(tmp_path, range_start=8, range_end=2)
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffmpeg.write_bytes(b"x")
    with pytest.raises(ValueError, match="开始时间"):
        validate_export(opt, ffmpeg)


def test_unique_output_path(tmp_path):
    first = tmp_path / "结果.mp4"
    first.write_bytes(b"x")
    assert unique_output_path(first) == tmp_path / "结果_1.mp4"
