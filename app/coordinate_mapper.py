"""预览坐标与原视频坐标换算。

所有函数只使用数值，便于脱离图形界面做单元测试。Qt 的高 DPI 坐标本身是
device-independent pixels；容器尺寸和鼠标位置使用同一坐标系，因此比例换算不会
混入 Windows 物理像素。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height


def calculate_video_content_rect(
    container_width: float,
    container_height: float,
    video_width: float,
    video_height: float,
) -> Rect:
    """返回视频按比例完整显示时，真实画面在控件中的矩形。"""
    if min(container_width, container_height, video_width, video_height) <= 0:
        return Rect(0, 0, 0, 0)
    scale = min(container_width / video_width, container_height / video_height)
    width = video_width * scale
    height = video_height * scale
    return Rect(
        (container_width - width) / 2,
        (container_height - height) / 2,
        width,
        height,
    )


def clamp_point_to_content(px: float, py: float, content: Rect) -> tuple[float, float]:
    return (
        min(max(px, content.x), content.right),
        min(max(py, content.y), content.bottom),
    )


def preview_to_video_point(
    px: float, py: float, content: Rect, video_width: int, video_height: int
) -> tuple[float, float]:
    if content.width <= 0 or content.height <= 0:
        raise ValueError("视频显示区域无效")
    px, py = clamp_point_to_content(px, py, content)
    return (
        (px - content.x) * video_width / content.width,
        (py - content.y) * video_height / content.height,
    )


def video_to_preview_rect(
    rect: Rect, content: Rect, video_width: int, video_height: int
) -> Rect:
    if video_width <= 0 or video_height <= 0:
        return Rect(0, 0, 0, 0)
    return Rect(
        content.x + rect.x * content.width / video_width,
        content.y + rect.y * content.height / video_height,
        rect.width * content.width / video_width,
        rect.height * content.height / video_height,
    )


def preview_to_video_rect(
    rect: Rect, content: Rect, video_width: int, video_height: int
) -> Rect:
    x1, y1 = preview_to_video_point(
        rect.x, rect.y, content, video_width, video_height
    )
    x2, y2 = preview_to_video_point(
        rect.right, rect.bottom, content, video_width, video_height
    )
    return Rect(x1, y1, x2 - x1, y2 - y1)


def normalize_video_rect(rect: Rect, video_width: int, video_height: int) -> Rect:
    x1 = min(max(rect.x, 0), video_width)
    y1 = min(max(rect.y, 0), video_height)
    x2 = min(max(rect.right, 0), video_width)
    y2 = min(max(rect.bottom, 0), video_height)
    return Rect(x1, y1, max(0, x2 - x1), max(0, y2 - y1))


def adjust_rect_for_yuv420p(
    rect: Rect,
    video_width: int,
    video_height: int,
    minimum: int = 4,
    delogo_margin: int = 2,
) -> tuple[int, int, int, int]:
    """轻微扩展/收缩为安全整数范围，尺寸尽量为偶数。

    FFmpeg delogo 会在矩形外读取一圈像素用于插值，所以矩形不能恰好贴住画面
    边缘。这里默认保留 2 像素安全边距，同时满足 yuv420p 常用偶数坐标。
    """
    if video_width < minimum + delogo_margin * 2:
        raise ValueError("视频宽度太小，无法创建安全的处理区域")
    if video_height < minimum + delogo_margin * 2:
        raise ValueError("视频高度太小，无法创建安全的处理区域")
    clean = normalize_video_rect(rect, video_width, video_height)
    safe_right = video_width - delogo_margin
    safe_bottom = video_height - delogo_margin
    x1 = max(delogo_margin, int(clean.x) // 2 * 2)
    y1 = max(delogo_margin, int(clean.y) // 2 * 2)
    x2 = min(safe_right, (int(clean.right + 1) // 2) * 2)
    y2 = min(safe_bottom, (int(clean.bottom + 1) // 2) * 2)
    if x2 - x1 < minimum:
        x2 = min(safe_right, x1 + minimum)
        x1 = max(delogo_margin, x2 - minimum)
    if y2 - y1 < minimum:
        y2 = min(safe_bottom, y1 + minimum)
        y1 = max(delogo_margin, y2 - minimum)
    width, height = x2 - x1, y2 - y1
    if width < minimum or height < minimum:
        raise ValueError("选框靠近视频边缘且过小，请稍微扩大选框")
    return x1, y1, width, height
