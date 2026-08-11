"""时间格式解析和显示。"""

from __future__ import annotations

import re

_TIME_RE = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$")


def parse_time(value: str) -> float:
    """把 HH:MM:SS.mmm 转为秒，不接受模糊格式。"""
    match = _TIME_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("时间格式应为 HH:MM:SS.mmm，例如 00:00:05.000")
    hours, minutes, seconds, milliseconds = match.groups()
    ms = int((milliseconds or "0").ljust(3, "0"))
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + ms / 1000


def format_time(seconds: float) -> str:
    total_ms = max(0, round(float(seconds) * 1000))
    hours, remain = divmod(total_ms, 3_600_000)
    minutes, remain = divmod(remain, 60_000)
    secs, ms = divmod(remain, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def format_player_time(milliseconds: int) -> str:
    return format_time(milliseconds / 1000.0)[:-4]
