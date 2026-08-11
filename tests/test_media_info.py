from pathlib import Path

import pytest

from app.ffmpeg_locator import locate_ffmpeg
from app.media_info import probe_media


@pytest.mark.integration
def test_probe_horizontal_test_media():
    paths = locate_ffmpeg()
    assert paths is not None
    media = Path(__file__).parent.parent / "test_media" / "横屏测试视频.mp4"
    if not media.exists():
        pytest.skip("测试媒体尚未生成")
    info = probe_media(paths.ffprobe, media)
    assert (info.width, info.height) == (1280, 720)
    assert info.duration == pytest.approx(10, abs=0.1)
    assert info.video_codec == "h264"
    assert info.audio_codec == "aac"
