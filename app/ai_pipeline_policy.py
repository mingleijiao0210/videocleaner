"""Pure policy helpers for the accelerated local-AI video pipeline."""

from __future__ import annotations


def worker_time_range_args(
    range_start: float | None,
    range_end: float | None,
) -> list[str]:
    """Build worker arguments for an optional inclusive processing range."""

    if (range_start is None) != (range_end is None):
        raise ValueError("开始时间和结束时间必须同时设置。")
    if range_start is None or range_end is None:
        return []
    return [
        "--range-start",
        f"{range_start:.6f}",
        "--range-end",
        f"{range_end:.6f}",
    ]


def worker_video_is_final_encode(effect_mode: str) -> bool:
    """Only the accelerated strong worker emits final-quality H.264."""

    return effect_mode == "ai_strong"
