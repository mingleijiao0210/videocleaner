"""本地智能精确去字：字形遮罩、逐帧修复和异步视频导出。"""

from __future__ import annotations

import os
import subprocess
import threading
import warnings
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import QThread, Signal

from .coordinate_mapper import Rect
from .ffmpeg_runner import (
    ExportOptions,
    all_selections,
    should_copy_audio,
    video_encoder_args,
    validate_export,
)
from .logger import LOGGER


def _odd(value: int, minimum: int = 3, maximum: int = 31) -> int:
    value = max(minimum, min(maximum, value))
    return value if value % 2 else value + 1


@dataclass(frozen=True)
class TextMaskConfig:
    """文字遮罩参数，默认值偏向保留背景、覆盖完整笔画。"""

    minimum_contrast: int = 6
    response_percentile: float = 66.0
    dilation: int = 3
    inpaint_radius: int = 4
    max_component_ratio: float = 0.65


def _filter_text_components(
    mask: np.ndarray, max_component_ratio: float = 0.65
) -> np.ndarray:
    """去掉长边框、噪点和明显不可能是文字的连通区域。"""

    height, width = mask.shape
    image_area = max(height * width, 1)
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    kept = np.zeros_like(mask)
    for index in range(1, count):
        x, y, component_width, component_height, area = stats[index]
        if area < 3 or area > image_area * max_component_ratio:
            continue
        if component_width < 2 or component_height < 3:
            continue
        if component_width > width * 0.96 and component_height < height * 0.16:
            continue
        if component_height > height * 0.92 and component_width < width * 0.10:
            continue
        fill_ratio = area / max(component_width * component_height, 1)
        if fill_ratio < 0.035:
            continue
        touches_edge = (
            x == 0
            or y == 0
            or x + component_width >= width
            or y + component_height >= height
        )
        if touches_edge and (
            component_width > width * 0.72 or component_height > height * 0.72
        ):
            continue
        kept[labels == index] = 255
    return kept


def _detect_structural_lines(gray: np.ndarray) -> np.ndarray:
    """识别文字框等长直边，避免把现有边框当作文字笔画。"""

    height, width = gray.shape
    protected = np.zeros_like(gray)
    edges = cv2.Canny(gray, 40, 130)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=max(18, min(width, height) // 5),
        minLineLength=max(18, min(width, height) // 4),
        maxLineGap=3,
    )
    if lines is None:
        return protected
    # OpenCV 4 commonly returns (N, 1, 4), while OpenCV 5 can return
    # (N, 4). Normalize both layouts before unpacking line endpoints.
    for item in np.asarray(lines).reshape(-1, 4):
        x1, y1, x2, y2 = map(int, item)
        dx = abs(x2 - x1)
        dy = abs(y2 - y1)
        if dy <= 2 and dx >= width * 0.22:
            cv2.line(protected, (x1, y1), (x2, y2), 255, 7)
        elif dx <= 2 and dy >= height * 0.38:
            cv2.line(protected, (x1, y1), (x2, y2), 255, 7)
    return protected


def detect_text_mask(
    roi: np.ndarray, config: TextMaskConfig | None = None
) -> np.ndarray:
    """在用户选框内生成字形级遮罩，不把整个矩形视作待修复区域。"""

    if roi is None or roi.size == 0:
        raise ValueError("文字区域图像为空。")
    config = config or TextMaskConfig()
    height, width = roi.shape[:2]
    if height < 4 or width < 4:
        return np.zeros((height, width), np.uint8)

    lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    protected_lines = _detect_structural_lines(gray)
    local_kernel = _odd(min(height, width) // 7, 7, 31)
    local_background = cv2.medianBlur(lab, local_kernel)
    local_difference = cv2.absdiff(lab, local_background).max(axis=2)

    shape_width = _odd(width // 18, 7, 31)
    shape_height = _odd(height // 9, 3, 11)
    shape_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (shape_width, shape_height)
    )
    black_hat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, shape_kernel)
    top_hat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, shape_kernel)
    response = np.maximum(local_difference, np.maximum(black_hat, top_hat))

    nonzero = response[response > 0]
    if nonzero.size == 0:
        return np.zeros((height, width), np.uint8)
    threshold = max(
        config.minimum_contrast,
        int(np.percentile(nonzero, config.response_percentile)),
    )
    candidate = np.where(response >= threshold, 255, 0).astype(np.uint8)

    # 彩色字幕在浅色背景上可能亮度差很小，但饱和度明显高于周围。
    saturation = hsv[:, :, 1]
    saturation_threshold = max(30, int(np.median(saturation)) + 16)
    colorful = (
        (saturation >= saturation_threshold)
        & (hsv[:, :, 2] >= 45)
        & (response >= max(3, threshold // 3))
    )
    candidate[colorful] = 255

    # 不再限制为红/黄等固定颜色：对 HSV 饱和度、LAB 色度通道做局部
    # 背景差分，覆盖绿色、青色、蓝色、紫色以及渐变字。
    for channel in (hsv[:, :, 1], lab[:, :, 1], lab[:, :, 2]):
        channel_background = cv2.medianBlur(channel, local_kernel)
        chroma_difference = cv2.absdiff(channel, channel_background)
        chroma_values = chroma_difference[chroma_difference > 0]
        if chroma_values.size:
            chroma_threshold = max(4, int(np.percentile(chroma_values, 58)))
            candidate[chroma_difference >= chroma_threshold] = 255

    # 彩色描边文字的中心可能亮度差较小，用边缘补齐这些笔画。
    median_gray = float(np.median(gray))
    canny_low = max(20, int(median_gray * 0.45))
    canny_high = max(canny_low + 20, int(median_gray * 1.15))
    edges = cv2.Canny(gray, canny_low, min(canny_high, 255))
    for channel in (hsv[:, :, 1], lab[:, :, 1], lab[:, :, 2]):
        channel_low = max(8, int(np.percentile(channel, 12)))
        channel_high = max(channel_low + 18, int(np.percentile(channel, 82)))
        edges = cv2.bitwise_or(
            edges,
            cv2.Canny(channel, channel_low, min(channel_high, 255)),
        )
    edge_gate = np.where(response >= max(6, threshold // 2), 255, 0).astype(
        np.uint8
    )
    candidate = cv2.bitwise_or(candidate, cv2.bitwise_and(edges, edge_gate))
    candidate = cv2.morphologyEx(
        candidate,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
    )
    # 文字框的长直边不是待删除文字，先剔除横线和竖线。
    horizontal_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(31, round(width * 0.70)), 1)
    )
    vertical_kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(31, round(height * 0.70)))
    )
    long_lines = cv2.bitwise_or(
        cv2.morphologyEx(candidate, cv2.MORPH_OPEN, horizontal_kernel),
        cv2.morphologyEx(candidate, cv2.MORPH_OPEN, vertical_kernel),
    )
    if cv2.countNonZero(long_lines):
        long_lines = cv2.dilate(
            long_lines,
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
            iterations=1,
        )
        candidate[long_lines > 0] = 0
    candidate = _filter_text_components(candidate, config.max_component_ratio)
    if config.dilation > 0:
        candidate = cv2.dilate(
            candidate,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
            iterations=config.dilation,
        )
    candidate[protected_lines > 0] = 0
    return candidate


class TemporalRepair:
    """优先使用近期无文字像素，不能可靠复用时回退到局部修复。"""

    def __init__(self, history_size: int = 8) -> None:
        self._history: deque[tuple[np.ndarray, np.ndarray]] = deque(
            maxlen=history_size
        )

    def repair(
        self,
        roi: np.ndarray,
        mask: np.ndarray,
        inpaint_radius: int = 4,
    ) -> np.ndarray:
        if cv2.countNonZero(mask) == 0:
            self._history.append((roi.copy(), mask.copy()))
            return roi

        repaired = cv2.inpaint(roi, mask, inpaint_radius, cv2.INPAINT_TELEA)
        if len(self._history) >= 3:
            frames = np.stack([item[0] for item in self._history]).astype(np.float32)
            masks = np.stack([item[1] for item in self._history])
            valid = masks == 0
            valid_count = valid.sum(axis=0)
            values = np.where(valid[..., None], frames, np.nan)
            with warnings.catch_warnings(), np.errstate(invalid="ignore"):
                warnings.simplefilter("ignore", RuntimeWarning)
                temporal = np.nanmedian(values, axis=0)
                spread = np.nanstd(values, axis=0).max(axis=2)
            safe = (
                (mask > 0)
                & (valid_count >= 3)
                & np.isfinite(temporal).all(axis=2)
                & (spread < 16.0)
            )
            repaired[safe] = np.clip(temporal[safe], 0, 255).astype(np.uint8)

        self._history.append((roi.copy(), mask.copy()))
        return repaired


def repair_frame_region(
    frame: np.ndarray,
    selection: Rect,
    temporal: TemporalRepair | None = None,
    config: TextMaskConfig | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """修复一帧的选区，并返回整帧结果和选区遮罩。"""

    height, width = frame.shape[:2]
    x1 = max(0, min(width - 1, round(selection.x)))
    y1 = max(0, min(height - 1, round(selection.y)))
    x2 = max(x1 + 1, min(width, round(selection.right)))
    y2 = max(y1 + 1, min(height, round(selection.bottom)))
    roi = frame[y1:y2, x1:x2]
    config = config or TextMaskConfig()
    mask = detect_text_mask(roi, config)
    engine = temporal or TemporalRepair(history_size=0)
    repaired_roi = engine.repair(roi, mask, config.inpaint_radius)
    result = frame.copy()
    result[y1:y2, x1:x2] = repaired_roi
    return result, mask


def generate_smart_preview(
    input_path: Path,
    position: float,
    selection: Rect | tuple[Rect, ...],
    destination: Path,
) -> Path:
    """生成智能模式预览；绿色半透明区域表示实际识别出的文字笔画。"""

    capture = cv2.VideoCapture(str(input_path))
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, position) * 1000.0)
        ok, frame = capture.read()
        if not ok or frame is None:
            raise RuntimeError("无法读取当前位置的视频画面。")
    finally:
        capture.release()
    selections = selection if isinstance(selection, tuple) else (selection,)
    preview = frame.copy()
    for rect in selections:
        repaired, mask = repair_frame_region(preview, rect)
        x1 = max(0, min(frame.shape[1] - 1, round(rect.x)))
        y1 = max(0, min(frame.shape[0] - 1, round(rect.y)))
        x2 = min(frame.shape[1], x1 + mask.shape[1])
        y2 = min(frame.shape[0], y1 + mask.shape[0])
        preview = repaired
        area = preview[y1:y2, x1:x2]
        active = mask[: y2 - y1, : x2 - x1] > 0
        if np.any(active):
            green = np.zeros_like(area)
            green[:, :] = (40, 220, 40)
            area[active] = cv2.addWeighted(
                area[active],
                0.55,
                green[active],
                0.45,
                0,
            )
        cv2.rectangle(
            preview,
            (x1, y1),
            (x2 - 1, y2 - 1),
            (0, 255, 0),
            2,
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(destination), preview):
        raise RuntimeError("无法保存智能处理预览图。")
    return destination


class SmartTextRunner(QThread):
    """在工作线程中逐帧生成文字遮罩并把结果送给 FFmpeg 编码。"""

    progressChanged = Signal(float, float, float)
    statusChanged = Signal(str)
    errorOccurred = Signal(str)
    completed = Signal(str)
    cancelled = Signal()

    def __init__(self, ffmpeg: Path, parent=None) -> None:
        super().__init__(parent)
        self.ffmpeg = Path(ffmpeg)
        self._options: ExportOptions | None = None
        self._cancel_event = threading.Event()
        self._process: subprocess.Popen | None = None

    @property
    def running(self) -> bool:
        return self.isRunning()

    def start_with(self, options: ExportOptions) -> None:
        if self.isRunning():
            raise RuntimeError("已有智能处理任务正在运行。")
        validate_export(options, self.ffmpeg)
        self._options = options
        self._cancel_event.clear()
        self.start()

    def cancel(self) -> None:
        if not self.isRunning():
            return
        LOGGER.info("用户取消智能精确去字")
        self.statusChanged.emit("正在取消智能处理…")
        self._cancel_event.set()
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _encoding_command(
        self, options: ExportOptions, width: int, height: int, fps: float
    ) -> list[str]:
        command = [
            str(self.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s:v",
            f"{width}x{height}",
            "-r",
            f"{fps:.8f}",
            "-i",
            "pipe:0",
            "-i",
            str(options.input_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
        ]
        command.extend(video_encoder_args(options))
        if options.audio_codec:
            if should_copy_audio(options):
                command.extend(["-c:a", "copy"])
            else:
                command.extend(["-c:a", "aac", "-b:a", "192k"])
        else:
            command.append("-an")
        command.extend(
            [
                "-map_metadata",
                "1",
                "-movflags",
                "+faststart",
                str(options.output_path),
            ]
        )
        return command

    def run(self) -> None:
        options = self._options
        if options is None:
            return
        capture: cv2.VideoCapture | None = None
        error_log = options.output_path.with_name(
            f".{options.output_path.stem}_smart_ffmpeg.log"
        )
        try:
            self.statusChanged.emit("正在分析文字笔画并修复视频…")
            capture = cv2.VideoCapture(str(options.input_path))
            if not capture.isOpened():
                raise RuntimeError("无法打开视频进行智能处理。")
            if hasattr(cv2, "CAP_PROP_ORIENTATION_AUTO"):
                capture.set(cv2.CAP_PROP_ORIENTATION_AUTO, 0)
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            if width <= 0 or height <= 0:
                raise RuntimeError("无法读取视频分辨率。")
            if fps <= 0 or not np.isfinite(fps):
                fps = frame_count / options.duration if frame_count > 0 else 25.0
            if (width, height) != (options.video_width, options.video_height):
                raise RuntimeError(
                    "智能处理读取到的视频方向与预览不一致，请先转换为 H.264 MP4 后重试。"
                )

            command = self._encoding_command(options, width, height, fps)
            LOGGER.info("智能精确去字 FFmpeg 命令: %s", subprocess.list2cmdline(command))
            options.output_path.parent.mkdir(parents=True, exist_ok=True)
            with error_log.open("wb") as stderr_file:
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if self._process.stdin is None:
                    raise RuntimeError("无法建立视频编码管道。")
                selections = all_selections(options)
                temporals = [TemporalRepair() for _selection in selections]
                index = 0
                while not self._cancel_event.is_set():
                    ok, frame = capture.read()
                    if not ok:
                        break
                    seconds = index / fps
                    in_range = (
                        options.range_start is None
                        or (
                            options.range_end is not None
                            and options.range_start <= seconds <= options.range_end
                        )
                    )
                    if in_range:
                        for selection, temporal in zip(selections, temporals):
                            frame, _mask = repair_frame_region(
                                frame,
                                selection,
                                temporal,
                            )
                    try:
                        self._process.stdin.write(frame.tobytes())
                    except (BrokenPipeError, OSError) as exc:
                        if self._cancel_event.is_set():
                            break
                        raise RuntimeError("视频编码进程意外结束。") from exc
                    index += 1
                    if index % 3 == 0:
                        percent = min(100.0, seconds / max(options.duration, 0.001) * 100)
                        self.progressChanged.emit(percent, seconds, options.duration)

                try:
                    self._process.stdin.close()
                except OSError:
                    pass
                if self._cancel_event.is_set():
                    if self._process.poll() is None:
                        self._process.terminate()
                    try:
                        self._process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=2)
                    options.output_path.unlink(missing_ok=True)
                    self.cancelled.emit()
                    return
                exit_code = self._process.wait()
            if exit_code != 0 or not options.output_path.is_file():
                detail = error_log.read_text(encoding="utf-8", errors="replace")[-6000:]
                raise RuntimeError(detail.strip() or "FFmpeg 编码失败。")
            LOGGER.info("智能精确去字完成: %s", options.output_path)
            self.progressChanged.emit(100.0, options.duration, options.duration)
            self.completed.emit(str(options.output_path))
        except Exception:
            LOGGER.exception("智能精确去字失败")
            try:
                options.output_path.unlink(missing_ok=True)
            except OSError:
                pass
            if self._cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.errorOccurred.emit(
                    "智能精确去字失败，请查看日志；也可以改用“插值去除”模式。"
                )
        finally:
            if capture is not None:
                capture.release()
            process = self._process
            if process is not None and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._process = None
            try:
                error_log.unlink(missing_ok=True)
            except OSError:
                pass
