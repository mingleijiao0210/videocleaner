"""在 VSR 自带 Python 环境中运行的 VideoCleaner 桥接脚本。"""

from __future__ import annotations

import argparse
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path


class _FramePrefetcher:
    """Decode upcoming frames while MPS is busy with the current batch."""

    def __init__(self, capture, buffer_size: int) -> None:
        self._capture = capture
        self._buffer = queue.Queue(maxsize=max(2, buffer_size))
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        while not self._stopped.is_set():
            item = self._capture.read()
            while not self._stopped.is_set():
                try:
                    self._buffer.put(item, timeout=0.1)
                    break
                except queue.Full:
                    continue
            if not item[0]:
                break

    def read(self):
        return self._buffer.get()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=2)


class _FFmpegVideoWriter:
    """Stream BGR frames directly to the native H.264 encoder."""

    def __init__(
        self,
        ffmpeg: Path,
        output: Path,
        fps: float,
        size: tuple[int, int],
    ) -> None:
        command = _ffmpeg_writer_command(
            ffmpeg,
            output,
            fps,
            size,
            platform_name=sys.platform,
        )
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._released = False

    def write(self, frame) -> None:
        if self._released:
            raise RuntimeError("视频编码器已经关闭。")
        if self._process.stdin is None:
            raise RuntimeError("无法写入 FFmpeg 视频管道。")
        try:
            self._process.stdin.write(frame.tobytes())
        except BrokenPipeError as exc:
            raise RuntimeError(self._error_text() or "FFmpeg 视频编码提前退出。") from exc

    def _error_text(self) -> str:
        if self._process.stderr is None:
            return ""
        return self._process.stderr.read().decode("utf-8", errors="replace").strip()

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except BrokenPipeError:
                pass
        exit_code = self._process.wait()
        if exit_code:
            raise RuntimeError(
                self._error_text() or f"FFmpeg 视频编码失败，退出代码 {exit_code}。"
            )


def _ffmpeg_writer_command(
    ffmpeg: Path,
    output: Path,
    fps: float,
    size: tuple[int, int],
    platform_name: str,
) -> list[str]:
    """Build the one-pass native encoding command without importing AI libs."""

    width, height = size
    encoder_args = (
        [
            "-c:v",
            "h264_videotoolbox",
            "-q:v",
            "70",
            "-allow_sw",
            "1",
        ]
        if platform_name == "darwin"
        else [
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
        ]
    )
    return [
        str(ffmpeg),
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
        f"{fps:.12g}",
        "-i",
        "pipe:0",
        "-an",
        *encoder_args,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]


def _frame_in_time_range(
    frame_number: int,
    fps: float,
    range_start: float | None,
    range_end: float | None,
) -> bool:
    """Match FFmpeg between(T,start,end) for a one-based CFR frame number."""

    if range_start is None or range_end is None:
        return True
    timestamp = max(0, frame_number - 1) / max(fps, 0.001)
    return range_start <= timestamp <= range_end


def _propagate_boxes_backward(
    detected: dict[int, list[tuple[int, int, int, int]]],
    lead_frames: int = 12,
) -> dict[int, list[tuple[int, int, int, int]]]:
    """把未来完整文字框回填到字幕淡入阶段。"""

    padded = {frame_no: list(boxes) for frame_no, boxes in detected.items()}
    for frame_no in sorted(detected):
        boxes = detected[frame_no]
        for target in range(max(1, frame_no - lead_frames), frame_no):
            target_boxes = padded.setdefault(target, [])
            for box in boxes:
                if box not in target_boxes:
                    target_boxes.append(box)
    return dict(sorted(padded.items()))


def _propagate_boxes_temporally(
    detected: dict[int, list[tuple[int, int, int, int]]],
    lead_frames: int = 12,
    trail_frames: int = 12,
) -> dict[int, list[tuple[int, int, int, int]]]:
    """向字幕出现前后补框，覆盖淡入、淡出和短暂 OCR 漏检。"""

    padded = _propagate_boxes_backward(detected, lead_frames=lead_frames)
    last_frame = max(detected, default=0) + max(0, trail_frames)
    for frame_no in sorted(detected):
        boxes = detected[frame_no]
        for target in range(frame_no + 1, min(last_frame, frame_no + trail_frames) + 1):
            target_boxes = padded.setdefault(target, [])
            for box in boxes:
                if box not in target_boxes:
                    target_boxes.append(box)
    return dict(sorted(padded.items()))


def _feather_inpaint_boundaries(
    input_frames,
    repaired_frames,
    input_mask,
    feather_pixels: int = 4,
):
    """在遮罩内缘平滑混合原帧与时序修复帧，消除硬边和色块感。"""

    import cv2
    import numpy as np

    binary_mask = (input_mask > 127).astype(np.uint8)
    if not np.any(binary_mask) or feather_pixels <= 0:
        return repaired_frames
    distance = cv2.distanceTransform(
        binary_mask,
        cv2.DIST_L2,
        3,
    )
    alpha = np.clip(distance / float(feather_pixels), 0.0, 1.0)
    alpha = alpha[:, :, None].astype(np.float32)
    feathered_frames = []
    for source, repaired in zip(input_frames, repaired_frames):
        blended = (
            repaired.astype(np.float32) * alpha
            + source.astype(np.float32) * (1.0 - alpha)
        )
        feathered_frames.append(np.clip(blended, 0, 255).astype(np.uint8))
    return feathered_frames


def _refine_overlay_text_mask(
    frames,
    rectangle_mask,
    outline_pixels: int = 3,
):
    """Reduce OCR rectangles to stable bright/colored character strokes.

    OCR returns bounding rectangles, but replacing every pixel in those
    rectangles creates an obvious mosaic on grass, water, and foliage.  Text
    overlays are usually bright white or saturated colors and stay fixed on
    screen while the background moves.  Voting across a temporal batch keeps
    those stable strokes, then a small dilation includes their dark outline.
    """

    import cv2
    import numpy as np

    restriction = (np.asarray(rectangle_mask) > 127).astype(np.uint8)
    if not np.any(restriction) or not frames:
        return np.asarray(rectangle_mask, dtype=np.uint8)

    votes = np.zeros(restriction.shape, dtype=np.uint16)
    for frame in frames:
        # Do not whitelist only white/yellow/red text.  A green, cyan,
        # magenta, blue or gradient glyph can have nearly the same luminance
        # as its background.  Use local contrast in all LAB channels and in
        # saturation, plus edges from every channel.  This is intentionally
        # colour-agnostic and works for Latin, CJK and other scripts because
        # it detects strokes rather than language-specific characters.
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        saturation = hsv[:, :, 1]
        channels = [gray, saturation, lab[:, :, 1], lab[:, :, 2]]
        kernel_size = max(3, min(15, int(round(min(frame.shape[:2]) / 12))))
        if kernel_size % 2 == 0:
            kernel_size += 1
        local_differences = []
        edge_mask = np.zeros(restriction.shape, dtype=np.uint8)
        for channel in channels:
            background = cv2.medianBlur(channel, kernel_size)
            local_differences.append(cv2.absdiff(channel, background))
            low = max(10, int(np.percentile(channel, 18)))
            high = max(low + 18, int(np.percentile(channel, 82)))
            edge_mask = cv2.bitwise_or(
                edge_mask,
                cv2.Canny(channel, low, min(high, 255)),
            )
        contrast = np.maximum.reduce(local_differences)
        inside = restriction > 0
        contrast_values = contrast[inside]
        contrast_threshold = max(
            5,
            int(np.percentile(contrast_values, 62)) if contrast_values.size else 5,
        )
        chroma = np.maximum(local_differences[1], local_differences[2])
        colorful = (saturation >= np.median(saturation[inside]) + 6) & (chroma >= 4)
        # Morphological top-hat/black-hat catches thin outlined glyphs whose
        # fill is close to the local background colour.
        stroke_kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT,
            (max(5, kernel_size * 2 + 1), max(3, kernel_size // 2 * 2 + 1)),
        )
        stroke_response = np.maximum(
            cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, stroke_kernel),
            cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, stroke_kernel),
        )
        stroke_threshold = max(
            5,
            int(np.percentile(stroke_response[inside], 68))
            if np.any(inside)
            else 5,
        )
        candidate = (
            (
                (contrast >= contrast_threshold)
                | colorful
                | (stroke_response >= stroke_threshold)
                | (edge_mask > 0)
            )
            & inside
        ).astype(np.uint8)
        candidate = cv2.morphologyEx(
            candidate,
            cv2.MORPH_OPEN,
            np.ones((2, 2), dtype=np.uint8),
        )
        candidate = cv2.morphologyEx(
            candidate,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
        )
        votes += candidate

    # Text can animate, shimmer or change colour.  A lower temporal vote
    # still rejects most moving background detail while retaining transient
    # coloured glyphs that are visible in only part of a batch.
    required_votes = max(1, int(len(frames) * 0.35 + 0.999))
    core = (votes >= required_votes).astype(np.uint8)

    component_count, labels, stats, _ = cv2.connectedComponentsWithStats(
        core,
        connectivity=8,
    )
    filtered = np.zeros_like(core)
    for label in range(1, component_count):
        if (
            stats[label, cv2.CC_STAT_AREA] >= 6
            and stats[label, cv2.CC_STAT_WIDTH] >= 2
            and stats[label, cv2.CC_STAT_HEIGHT] >= 2
        ):
            filtered[labels == label] = 1

    rectangle_area = int(np.count_nonzero(restriction))
    core_area = int(np.count_nonzero(filtered))
    # Unknown dark-only text falls back to the safe OCR rectangle.  A candidate
    # covering most of the rectangle is background, not a character mask.
    if core_area < max(3, round(rectangle_area * 0.002)):
        return restriction * 255
    if core_area > rectangle_area * 0.55:
        return restriction * 255

    radius = max(1, int(outline_pixels))
    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE,
        (radius * 2 + 1, radius * 2 + 1),
    )
    refined = cv2.dilate(filtered, kernel, iterations=1)
    refined &= restriction
    return refined.astype(np.uint8) * 255


def _mask_deviation_for_mode(mode: str) -> int:
    """不同模式的文字描边、阴影安全余量。"""

    if mode in {"propainter", "propainter_fast", "fast", "strong"}:
        return 12
    if mode == "precise":
        return 8
    return 0


def _safe_continuous_ranges(find_ranges, detected):
    """上游范围函数不接受空字典；没有文字时应安全返回空区间。"""

    if not detected:
        return []
    return find_ranges(detected)


def _safe_merge_intervals(merge_intervals, intervals):
    """没有字幕区间时跳过上游对首元素的访问。"""

    if not intervals:
        return []
    return merge_intervals(intervals)


def _strong_crop_rect(
    frame_width: int,
    frame_height: int,
    sub_area: tuple[int, int, int, int],
    target_aspect: float = 4.0,
) -> tuple[int, int, int, int]:
    """Create one stable, context-rich crop around the user's text selection."""

    ymin, ymax, xmin, xmax = sub_area
    xmin = max(0, min(frame_width - 1, xmin))
    xmax = max(xmin + 1, min(frame_width, xmax))
    ymin = max(0, min(frame_height - 1, ymin))
    ymax = max(ymin + 1, min(frame_height, ymax))
    width = xmax - xmin
    height = ymax - ymin

    padded_width = min(frame_width, width + max(48, round(width * 0.20)))
    padded_height = min(frame_height, height + max(32, round(height * 1.00)))
    if padded_width / max(padded_height, 1) < target_aspect:
        padded_width = min(frame_width, round(padded_height * target_aspect))
    else:
        padded_height = min(frame_height, round(padded_width / target_aspect))

    center_x = (xmin + xmax) / 2.0
    center_y = (ymin + ymax) / 2.0
    x1 = round(center_x - padded_width / 2.0)
    y1 = round(center_y - padded_height / 2.0)
    x1 = max(0, min(frame_width - padded_width, x1))
    y1 = max(0, min(frame_height - padded_height, y1))
    return x1, y1, x1 + padded_width, y1 + padded_height


def _create_cropped_box_mask(
    crop_rect: tuple[int, int, int, int],
    boxes: list[tuple[int, int, int, int]],
    deviation: int,
):
    """Create the same expanded rectangle mask directly in crop coordinates."""

    import cv2
    import numpy as np

    crop_x1, crop_y1, crop_x2, crop_y2 = crop_rect
    mask = np.zeros((crop_y2 - crop_y1, crop_x2 - crop_x1), dtype=np.uint8)
    for xmin, xmax, ymin, ymax in boxes:
        x1 = max(0, int(xmin) - deviation) - crop_x1
        y1 = max(0, int(ymin) - deviation) - crop_y1
        x2 = int(xmax) + deviation - crop_x1
        y2 = int(ymax) + deviation - crop_y1
        cv2.rectangle(mask, (x1, y1), (x2, y2), 255, thickness=-1)
    return mask


def _propainter_crop_rect(
    frame_width: int,
    frame_height: int,
    sub_area: tuple[int, int, int, int],
    max_width: int = 1280,
    max_height: int = 720,
) -> tuple[int, int, int, int]:
    """Context-rich, MPS-friendly crop whose dimensions are divisible by 8."""

    ymin, ymax, xmin, xmax = sub_area
    xmin = max(0, min(frame_width - 1, xmin))
    xmax = max(xmin + 1, min(frame_width, xmax))
    ymin = max(0, min(frame_height - 1, ymin))
    ymax = max(ymin + 1, min(frame_height, ymax))
    selected_width = xmax - xmin
    selected_height = ymax - ymin
    wanted_width = min(
        frame_width,
        max(selected_width + 192, round(selected_width * 1.35)),
    )
    wanted_height = min(
        frame_height,
        max(selected_height + 128, round(selected_height * 3.0)),
    )
    wanted_width = min(
        frame_width,
        max(selected_width, min(max_width, wanted_width)),
    )
    wanted_height = min(
        frame_height,
        max(selected_height, min(max_height, wanted_height)),
    )
    wanted_width = min(frame_width, ((wanted_width + 7) // 8) * 8)
    wanted_height = min(frame_height, ((wanted_height + 7) // 8) * 8)
    center_x = (xmin + xmax) / 2.0
    center_y = (ymin + ymax) / 2.0
    x1 = max(0, min(frame_width - wanted_width, round(center_x - wanted_width / 2)))
    y1 = max(0, min(frame_height - wanted_height, round(center_y - wanted_height / 2)))
    return x1, y1, x1 + wanted_width, y1 + wanted_height


def _balanced_propainter_crop_rect(
    frame_width: int,
    frame_height: int,
    sub_area: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Smaller context crop for the M2 Pro quality/speed balanced mode."""

    ymin, ymax, xmin, xmax = sub_area
    selected_width = max(1, xmax - xmin)
    selected_height = max(1, ymax - ymin)
    return _propainter_crop_rect(
        frame_width,
        frame_height,
        sub_area,
        max_width=min(960, max(selected_width + 128, round(selected_width * 1.25))),
        max_height=min(540, max(selected_height + 96, round(selected_height * 2.2))),
    )


def _balanced_inference_size(
    width: int,
    height: int,
    max_width: int = 960,
    max_height: int = 480,
) -> tuple[int, int]:
    """Fit a crop to an efficient MPS canvas while preserving aspect ratio."""

    scale = min(1.0, max_width / max(width, 1), max_height / max(height, 1))
    if scale >= 0.98:
        return width, height
    fitted_width = max(8, (round(width * scale) // 8) * 8)
    fitted_height = max(8, (round(height * scale) // 8) * 8)
    return min(width, fitted_width), min(height, fitted_height)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine-root", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--range-start", type=float)
    parser.add_argument("--range-end", type=float)
    parser.add_argument("--coords", nargs=4, required=True, type=int)
    parser.add_argument(
        "--mode",
        choices=("propainter", "propainter_fast", "strong", "fast", "precise", "full"),
        default="precise",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if (args.range_start is None) != (args.range_end is None):
        raise ValueError("--range-start 和 --range-end 必须同时提供。")
    # PaddleOCR 2.x 会再次解析全局 sys.argv；移除桥接层参数，避免被误判为
    # PaddleOCR 的未知命令行参数。
    sys.argv = [sys.argv[0]]
    engine_root = Path(args.engine_root).resolve()
    if args.ffmpeg:
        os.environ["VIDEOCLEANER_FFMPEG"] = str(Path(args.ffmpeg).resolve())
    os.chdir(engine_root)
    sys.path.insert(0, str(engine_root))
    sys.path.insert(0, str(engine_root / "backend"))

    # VSR 1.1.1 的 main.py 将 backend 目录中的 config 作为顶层模块导入。
    import config  # type: ignore
    from backend.main import SubtitleDetect, SubtitleRemover  # type: ignore

    input_path = Path(args.input).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ymin, ymax, xmin, xmax = args.coords
    selection_width = max(1, xmax - xmin)
    selection_height = max(1, ymax - ymin)
    import cv2

    probe_capture = cv2.VideoCapture(str(input_path))
    source_fps = float(probe_capture.get(cv2.CAP_PROP_FPS) or 0.0)
    probe_capture.release()

    if args.mode in {"propainter", "propainter_fast", "strong", "fast", "precise"}:
        original_get_coordinates = SubtitleDetect.get_coordinates

        def get_text_coordinates(dt_box):
            coordinates = original_get_coordinates(dt_box)
            filtered = []
            for box in coordinates:
                box_xmin, box_xmax, box_ymin, box_ymax = box
                box_width = box_xmax - box_xmin
                box_height = box_ymax - box_ymin
                # OCR 偶尔把白色矩形框整体识别成一个超高文本框。
                # 同时满足“横向很长、纵向占选区六成以上”的框按边框处理并保留。
                looks_like_container_border = (
                    box_width >= selection_width * 0.50
                    and box_height >= selection_height * 0.60
                )
                if not looks_like_container_border:
                    filtered.append(box)
            return filtered

        SubtitleDetect.get_coordinates = staticmethod(get_text_coordinates)

        original_find_ranges = SubtitleDetect.find_continuous_ranges_with_same_mask
        original_merge_intervals = SubtitleDetect.filter_and_merge_intervals

        def find_ranges_allowing_no_text(detected):
            return _safe_continuous_ranges(original_find_ranges, detected)

        def merge_intervals_allowing_no_text(intervals):
            return _safe_merge_intervals(original_merge_intervals, intervals)

        SubtitleDetect.find_continuous_ranges_with_same_mask = staticmethod(
            find_ranges_allowing_no_text
        )
        SubtitleDetect.filter_and_merge_intervals = staticmethod(
            merge_intervals_allowing_no_text
        )

        import backend.main as backend_main  # type: ignore

        original_sttn_call = backend_main.STTNInpaint.__call__

        def strong_sttn_call(self, input_frames, input_mask):
            repaired_frames = original_sttn_call(
                self,
                input_frames,
                input_mask,
            )
            return _feather_inpaint_boundaries(
                input_frames,
                repaired_frames,
                input_mask,
                feather_pixels=4,
            )

        backend_main.STTNInpaint.__call__ = strong_sttn_call

    if args.mode in {"propainter", "propainter_fast", "strong", "fast"}:
        original_detect_subtitle = SubtitleDetect.detect_subtitle

        def detect_subtitle_sampled(self, img):
            frame_index = getattr(self, "_videocleaner_frame_index", 0)
            cached_result = getattr(self, "_videocleaner_cached_detection", None)
            if not _frame_in_time_range(
                frame_index + 1,
                source_fps,
                args.range_start,
                args.range_end,
            ):
                import numpy as np

                self._videocleaner_frame_index = frame_index + 1
                return np.empty((0, 4, 2), dtype=np.float32), 0.0
            detection_stride = (
                3
                if args.mode == "propainter"
                else (5 if args.mode == "propainter_fast" else 6)
            )
            if cached_result is None or frame_index % detection_stride == 0:
                if self.sub_area is None:
                    cached_result = original_detect_subtitle(self, img)
                else:
                    import cv2
                    import numpy as np

                    area_ymin, area_ymax, area_xmin, area_xmax = self.sub_area
                    crop = img[area_ymin:area_ymax, area_xmin:area_xmax]
                    if crop.size == 0:
                        cached_result = original_detect_subtitle(self, img)
                    else:
                        # 只把用户选框送入 OCR，并放大后检测。竖屏视频若整帧
                        # 缩放，底部小字幕容易只识别到中间几个字。既然用户已
                        # 明确框选区域，就不再重复执行一次整帧 OCR。
                        scale = 2.0
                        enlarged = cv2.resize(
                            crop,
                            None,
                            fx=scale,
                            fy=scale,
                            interpolation=cv2.INTER_CUBIC,
                        )
                        boxes, elapsed = self.text_detector(enlarged)
                        adjusted = boxes.copy()
                        if getattr(adjusted, "size", 0):
                            adjusted[:, :, 0] = (
                                adjusted[:, :, 0] / scale + area_xmin
                            )
                            adjusted[:, :, 1] = (
                                adjusted[:, :, 1] / scale + area_ymin
                            )
                        cached_result = adjusted, elapsed
                self._videocleaner_cached_detection = cached_result
            self._videocleaner_frame_index = frame_index + 1
            return cached_result

        SubtitleDetect.detect_subtitle = detect_subtitle_sampled

        original_find_subtitle_frames = SubtitleDetect.find_subtitle_frame_no

        def find_subtitle_frames_with_lookahead(self, sub_remover=None):
            detected = original_find_subtitle_frames(self, sub_remover)
            if not detected:
                return detected
            # 将未来检测到的完整文字框回填给前 12 帧。这样字幕淡入或
            # 逐字出现时，不会继续使用上一组“无文字/不完整文字”结果。
            return _propagate_boxes_temporally(
                detected,
                lead_frames=12,
                trail_frames=12,
            )

        SubtitleDetect.find_subtitle_frame_no = (
            find_subtitle_frames_with_lookahead
        )

        # STTN 官方实现固定按 640x120 推理。低配机器上改用更小的内部画布，
        # 只影响模型计算量；最终仍按遮罩合成回原始视频分辨率。
        original_sttn_init = backend_main.STTNInpaint.__init__

        def fast_sttn_init(self):
            original_sttn_init(self)
            # 提高横向内部清晰度，减少小字和半透明描边在重建后重新显形。
            self.model_input_width = 400
            # 保留 120 像素纵向细节，避免小字在缩放时丢失笔画。
            self.model_input_height = 120
            self.neighbor_stride = 5
            self.ref_length = 20
            # 400x120 输入生成 100x30 特征图。保留完整 8 层时序建模，
            # 只调整各注意力分块以匹配新的内部尺寸。
            fast_patch_sizes = [(50, 15), (20, 6), (10, 5), (5, 3)]
            for block in self.model.transformer:
                block.attention.patchsize = fast_patch_sizes

        if args.mode == "fast":
            backend_main.STTNInpaint.__init__ = fast_sttn_init

    if args.mode == "strong":
        import backend.main as backend_main  # type: ignore

        def strong_lama_mode(self, tbar):
            """Run batched LaMa on one stable subtitle crop."""

            from collections import OrderedDict
            import cv2
            import numpy as np
            import torch

            sub_list = self.sub_detector.find_subtitle_frame_no(sub_remover=self)
            sub_list = {
                frame_no: boxes
                for frame_no, boxes in sub_list.items()
                if _frame_in_time_range(
                    frame_no,
                    self.fps,
                    args.range_start,
                    args.range_end,
                )
            }
            detected_boxes = [
                box for boxes in sub_list.values() for box in boxes
            ]
            if detected_boxes:
                detected_bounds = (
                    min(box[0] for box in detected_boxes),
                    max(box[1] for box in detected_boxes),
                    min(box[2] for box in detected_boxes),
                    max(box[3] for box in detected_boxes),
                )
            else:
                detected_bounds = None
                if self.sub_area is not None:
                    area_ymin, area_ymax, area_xmin, area_xmax = self.sub_area
                    fallback_box = (
                        area_xmin,
                        area_xmax,
                        area_ymin,
                        area_ymax,
                    )
                    sub_list = {
                        frame_no: [fallback_box]
                        for frame_no in range(1, int(self.frame_count) + 1)
                        if _frame_in_time_range(
                            frame_no,
                            self.fps,
                            args.range_start,
                            args.range_end,
                        )
                    }
            print(
                "VIDEOCLEANER_STRONG_DETECTION="
                f"frames:{len(sub_list)},bounds:{detected_bounds},"
                f"fallback:{detected_bounds is None}",
                flush=True,
            )
            print("[Processing] strong LaMa subtitle removal", flush=True)
            model_path = Path(config.LAMA_MODEL_PATH) / "big-lama.pt"
            use_mps = (
                sys.platform == "darwin"
                and hasattr(torch.backends, "mps")
                and torch.backends.mps.is_available()
            )
            device = torch.device("mps" if use_mps else "cpu")
            torch.set_num_threads(max(2, min(8, os.cpu_count() or 4)))
            model = torch.jit.load(str(model_path), map_location="cpu")
            model.to(device)
            model.eval()

            if self.sub_area is None:
                sub_area = (0, self.frame_height, 0, self.frame_width)
            else:
                sub_area = self.sub_area
            crop_x1, crop_y1, crop_x2, crop_y2 = _strong_crop_rect(
                self.frame_width,
                self.frame_height,
                sub_area,
            )
            target_width, target_height = (
                (768, 192) if use_mps else (384, 96)
            )
            # M2 Pro 32GB can keep 24 subtitle crops in unified memory.  The
            # larger batch halves Metal command submission overhead while
            # preserving the exact same model resolution, mask, and blending.
            batch_size = 24 if use_mps else 4
            frame_number = 0
            mask_cache = OrderedDict()

            def mask_assets(boxes, image_crop, temporal_crops=None):
                key = tuple(tuple(int(value) for value in box) for box in boxes)
                cached = mask_cache.pop(key, None)
                if cached is not None:
                    mask_cache[key] = cached
                    rectangle_mask = cached
                else:
                    rectangle_mask = _create_cropped_box_mask(
                        (crop_x1, crop_y1, crop_x2, crop_y2),
                        boxes,
                        int(config.SUBTITLE_AREA_DEVIATION_PIXEL),
                    )
                    mask_cache[key] = rectangle_mask
                    if len(mask_cache) > 32:
                        mask_cache.popitem(last=False)
                if not np.any(rectangle_mask):
                    return None
                mask_crop = _refine_overlay_text_mask(
                    temporal_crops or [image_crop],
                    rectangle_mask,
                    # 彩色字常带较粗黑描边和阴影；1.8.1 的 7px 余量在
                    # 个别帧仍会留下外轮廓。强力模式扩大到 10px，仍只
                    # 在候选笔画周围扩张，不会把整块选框送进模型。
                    outline_pixels=10,
                )
                mask_small = cv2.resize(
                    mask_crop,
                    (target_width, target_height),
                    interpolation=cv2.INTER_NEAREST,
                )
                mask_tensor = (mask_small > 127).astype(np.float32)[None, :, :]
                binary_alpha = (mask_crop > 127).astype(np.float32)
                feather_base = cv2.dilate(
                    binary_alpha,
                    cv2.getStructuringElement(
                        cv2.MORPH_ELLIPSE,
                        (3, 3),
                    ),
                    iterations=1,
                )
                alpha = cv2.GaussianBlur(
                    feather_base,
                    (0, 0),
                    1.25,
                )
                # Never mix the original lettering back into the character
                # core; feather only the pixels immediately outside it.
                alpha[binary_alpha > 0] = 1.0
                alpha = alpha[:, :, None]
                return mask_crop, mask_tensor, alpha

            def process_batch(frames, frame_numbers):
                active_indices = []
                image_tensors = []
                mask_tensors = []
                blend_alphas = {}
                for batch_index, (frame, number) in enumerate(
                    zip(frames, frame_numbers)
                ):
                    boxes = sub_list.get(number)
                    if not boxes:
                        continue
                    image_crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]
                    assets = mask_assets(boxes, image_crop)
                    if assets is None:
                        continue
                    mask_crop, mask_tensor, alpha = assets
                    image_rgb = cv2.cvtColor(image_crop, cv2.COLOR_BGR2RGB)
                    image_small = cv2.resize(
                        image_rgb,
                        (target_width, target_height),
                        interpolation=cv2.INTER_AREA,
                    )
                    active_indices.append(batch_index)
                    blend_alphas[batch_index] = alpha
                    image_tensors.append(
                        np.transpose(
                            image_small.astype(np.float32) / 255.0,
                            (2, 0, 1),
                        )
                    )
                    mask_tensors.append(mask_tensor)

                if active_indices:
                    images = torch.from_numpy(np.stack(image_tensors)).to(device)
                    masks = torch.from_numpy(np.stack(mask_tensors)).to(device)
                    with torch.inference_mode():
                        repaired = model(images, masks)
                    repaired = (
                        repaired.detach().cpu().permute(0, 2, 3, 1).numpy()
                    )
                    repaired = np.clip(
                        repaired * 255.0,
                        0,
                        255,
                    ).astype(np.uint8)
                    for result_index, batch_index in enumerate(active_indices):
                        frame = frames[batch_index]
                        source_crop = frame[crop_y1:crop_y2, crop_x1:crop_x2]
                        filled_rgb = cv2.resize(
                            repaired[result_index],
                            (crop_x2 - crop_x1, crop_y2 - crop_y1),
                            interpolation=cv2.INTER_CUBIC,
                        )
                        filled = cv2.cvtColor(filled_rgb, cv2.COLOR_RGB2BGR)
                        alpha = blend_alphas[batch_index]
                        blended = (
                            filled.astype(np.float32) * alpha
                            + source_crop.astype(np.float32) * (1.0 - alpha)
                        )
                        frame[crop_y1:crop_y2, crop_x1:crop_x2] = np.clip(
                            blended,
                            0,
                            255,
                        ).astype(np.uint8)

                for frame in frames:
                    self.video_writer.write(frame)
                tbar.update(len(frames))
                self.progress_remover = (
                    100.0
                    * float(frame_numbers[-1])
                    / float(self.frame_count)
                    // 2
                )
                self.progress_total = 50 + self.progress_remover

            frames = []
            frame_numbers = []
            reader = _FramePrefetcher(
                self.video_cap,
                buffer_size=batch_size * 2,
            )
            try:
                while True:
                    ret, frame = reader.read()
                    if not ret:
                        break
                    frame_number += 1
                    frames.append(frame)
                    frame_numbers.append(frame_number)
                    if len(frames) == batch_size:
                        process_batch(frames, frame_numbers)
                        frames, frame_numbers = [], []
                if frames:
                    process_batch(frames, frame_numbers)
            finally:
                reader.stop()

        backend_main.SubtitleRemover.lama_mode = strong_lama_mode

    if args.mode in {"propainter", "propainter_fast"}:
        import backend.main as backend_main  # type: ignore

        original_propainter_inpaint = backend_main.VideoInpaint.inpaint

        def cropped_propainter_inpaint(self, frames, mask):
            """Run ProPainter only on the selected band plus surrounding context."""

            frame_height, frame_width = frames[0].shape[:2]
            crop_function = (
                _balanced_propainter_crop_rect
                if args.mode == "propainter_fast"
                else _propainter_crop_rect
            )
            crop_x1, crop_y1, crop_x2, crop_y2 = crop_function(
                frame_width,
                frame_height,
                (ymin, ymax, xmin, xmax),
            )
            cropped_frames = [
                frame[crop_y1:crop_y2, crop_x1:crop_x2].copy()
                for frame in frames
            ]
            cropped_mask = mask[crop_y1:crop_y2, crop_x1:crop_x2].copy()
            cropped_mask = _refine_overlay_text_mask(
                cropped_frames,
                cropped_mask,
                outline_pixels=8,
            )
            inference_frames = cropped_frames
            inference_mask = cropped_mask
            inference_width = crop_x2 - crop_x1
            inference_height = crop_y2 - crop_y1
            if args.mode == "propainter_fast":
                import cv2

                self.raft_iter = 12
                self.neighbor_length = 8
                self.ref_stride = 12
                fitted_width, fitted_height = _balanced_inference_size(
                    inference_width,
                    inference_height,
                )
                if (fitted_width, fitted_height) != (
                    inference_width,
                    inference_height,
                ):
                    inference_frames = [
                        cv2.resize(
                            frame,
                            (fitted_width, fitted_height),
                            interpolation=cv2.INTER_AREA,
                        )
                        for frame in cropped_frames
                    ]
                    inference_mask = cv2.resize(
                        cropped_mask,
                        (fitted_width, fitted_height),
                        interpolation=cv2.INTER_NEAREST,
                    )
            repaired_crops = original_propainter_inpaint(
                self,
                inference_frames,
                inference_mask,
            )
            if args.mode == "propainter_fast" and (
                inference_frames[0].shape[1], inference_frames[0].shape[0]
            ) != (inference_width, inference_height):
                repaired_crops = [
                    cv2.resize(
                        frame,
                        (inference_width, inference_height),
                        interpolation=cv2.INTER_CUBIC,
                    )
                    for frame in repaired_crops
                ]
            if args.mode == "propainter_fast":
                repaired_crops = _feather_inpaint_boundaries(
                    cropped_frames,
                    repaired_crops,
                    cropped_mask,
                    feather_pixels=4,
                )
            results = []
            for source, repaired_crop in zip(frames, repaired_crops):
                result = source.copy()
                result[crop_y1:crop_y2, crop_x1:crop_x2] = repaired_crop
                results.append(result)
            return results

        backend_main.VideoInpaint.inpaint = cropped_propainter_inpaint

    remover = SubtitleRemover(
        str(input_path),
        sub_area=(ymin, ymax, xmin, xmax),
        gui_mode=False,
    )
    if args.mode == "strong" and args.ffmpeg:
        # Replace OpenCV's CPU mp4v writer with the Mac hardware encoder. The
        # outer runner restores audio with stream copy, so the worker must not
        # perform VSR's additional libx264 transcode.
        old_writer = remover.video_writer
        old_temp = remover.video_temp_file
        old_writer.release()
        old_temp_path = Path(old_temp.name)
        old_temp.close()
        old_temp_path.unlink(missing_ok=True)
        direct_temp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        direct_temp.close()
        remover.video_temp_file = direct_temp
        remover.video_writer = _FFmpegVideoWriter(
            Path(args.ffmpeg),
            Path(direct_temp.name),
            remover.fps,
            remover.size,
        )

        def copy_video_without_audio_transcode():
            import shutil

            shutil.copy2(remover.video_temp_file.name, remover.video_out_name)
            remover.is_successful_merged = True

        remover.merge_audio_to_video = copy_video_without_audio_transcode
    config.MODE = (
        config.InpaintMode.PROPAINTER
        if args.mode in {"propainter", "propainter_fast"}
        else (
            config.InpaintMode.LAMA
            if args.mode == "strong"
            else config.InpaintMode.STTN
        )
    )
    if args.mode in {"propainter", "propainter_fast"} and sys.platform == "darwin":
        # M2 Pro 32GB: 50-frame fp16 windows preserve strong temporal
        # context while leaving headroom for macOS and the OCR process.
        config.PROPAINTER_MAX_LOAD_NUM = 50
    config.STTN_SKIP_DETECTION = args.mode == "full"
    # 官方默认会把 OCR 框向四周扩 20 像素，容易把字幕框边缘一起擦掉。
    # 精准模式只保留 6 像素余量，足够覆盖描边文字，同时保护白色边框。
    config.SUBTITLE_AREA_DEVIATION_PIXEL = _mask_deviation_for_mode(args.mode)
    if os.name == "nt" and hasattr(config, "USE_DML") and args.mode != "strong":
        config.USE_DML = True
    remover.video_out_name = str(output_path)

    stopped = threading.Event()

    def report_progress() -> None:
        while not stopped.wait(0.5):
            value = max(0.0, min(100.0, float(remover.progress_total)))
            print(f"VIDEOCLEANER_PROGRESS={value:.3f}", flush=True)

    reporter = threading.Thread(target=report_progress, daemon=True)
    reporter.start()
    try:
        print(
            "VIDEOCLEANER_STATUS="
            + (
                (
                    "正在使用 M2 Pro Metal 执行 ProPainter 高质量快速修复"
                    if args.mode == "propainter_fast"
                    else "正在使用 M2 Pro Metal 执行 ProPainter 专业时序修复"
                )
                if args.mode in {"propainter", "propainter_fast"}
                else (
                    "正在逐帧提取文字笔画并融合真实背景"
                    if args.mode == "strong"
                    else (
                        "正在快速检测文字并进行时序修复"
                        if args.mode == "fast"
                        else (
                            "正在检测文字并进行时序修复"
                            if args.mode == "precise"
                            else "正在重建整个选框"
                        )
                    )
                )
            ),
            flush=True,
        )
        remover.run()
        print("VIDEOCLEANER_PROGRESS=100.000", flush=True)
        print(f"VIDEOCLEANER_OUTPUT={output_path}", flush=True)
        return 0
    except BaseException:
        print("VIDEOCLEANER_ERROR_BEGIN", flush=True)
        traceback.print_exc()
        print("VIDEOCLEANER_ERROR_END", flush=True)
        return 1
    finally:
        stopped.set()
        reporter.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit(main())
