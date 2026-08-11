"""使用 ffprobe 读取视频信息。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    width: int
    height: int
    duration: float
    frame_rate: float
    frame_rate_text: str
    video_codec: str
    audio_codec: str | None
    file_size: int

    @property
    def has_audio(self) -> bool:
        return bool(self.audio_codec)


def _rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    if "/" in value:
        left, right = value.split("/", 1)
        return float(left) / float(right) if float(right) else 0.0
    return float(value)


def _rotation(stream: dict) -> int:
    tag_value = (stream.get("tags") or {}).get("rotate")
    if tag_value is not None:
        try:
            return int(round(float(tag_value)))
        except (TypeError, ValueError):
            pass
    for side_data in stream.get("side_data_list") or []:
        if "rotation" in side_data:
            try:
                return int(round(float(side_data["rotation"])))
            except (TypeError, ValueError):
                pass
    return 0


def probe_media(ffprobe: Path, media_path: Path) -> MediaInfo:
    media_path = Path(media_path)
    if not media_path.is_file():
        raise FileNotFoundError("选择的视频文件不存在。")
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(media_path),
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=30,
    )
    if result.returncode != 0:
        raise ValueError(
            "无法读取视频信息。文件可能损坏或格式不受支持。"
            + (f"\n{result.stderr.strip()}" if result.stderr.strip() else "")
        )
    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError("文件中没有可用的视频轨道。")
    format_data = data.get("format", {})
    duration_text = video.get("duration") or format_data.get("duration") or "0"
    duration = float(duration_text)
    rate_text = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"
    size = int(format_data.get("size") or media_path.stat().st_size)
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    if abs(_rotation(video)) % 180 == 90:
        width, height = height, width
    return MediaInfo(
        path=media_path.resolve(),
        width=width,
        height=height,
        duration=duration,
        frame_rate=_rate(rate_text),
        frame_rate_text=rate_text,
        video_codec=video.get("codec_name") or "未知",
        audio_codec=(audio or {}).get("codec_name"),
        file_size=size,
    )
