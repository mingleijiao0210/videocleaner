"""FSRCNN x2 video worker used by VideoCleaner.

Frames are decoded with OpenCV, enlarged by the bundled FSRCNN network and
streamed directly to the bundled FFmpeg encoder.  The source audio is restored
as AAC so the resulting MP4 is broadly compatible.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import cv2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    return parser


def _encoder_args() -> list[str]:
    if sys.platform == "darwin":
        return [
            "-c:v",
            "h264_videotoolbox",
            "-q:v",
            "70",
            "-pix_fmt",
            "yuv420p",
        ]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
    ]


def main() -> int:
    args = _parser().parse_args()
    for path, title in (
        (args.input, "输入视频"),
        (args.model, "FSRCNN 模型"),
        (args.ffmpeg, "FFmpeg"),
    ):
        if not path.is_file():
            raise FileNotFoundError(f"{title}不存在：{path}")

    capture = cv2.VideoCapture(str(args.input))
    if not capture.isOpened():
        raise RuntimeError("OpenCV 无法打开输入视频。")
    width = round(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = round(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    frame_count = max(0, round(capture.get(cv2.CAP_PROP_FRAME_COUNT)))
    if width <= 0 or height <= 0 or fps <= 0:
        raise RuntimeError("输入视频的尺寸或帧率无效。")
    if width * 2 > 3840 or height * 2 > 3840 or width * height * 4 > 3840 * 2160:
        raise ValueError("AI 2× 超清的输出不能超过 4K，请改用快速清晰增强。")

    cv2.setNumThreads(max(2, min(8, os.cpu_count() or 4)))
    super_resolution = cv2.dnn_superres.DnnSuperResImpl_create()
    super_resolution.readModel(str(args.model))
    super_resolution.setModel("fsrcnn", 2)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(args.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-video_size",
        f"{width * 2}x{height * 2}",
        "-framerate",
        f"{fps:.8f}",
        "-i",
        "pipe:0",
        "-i",
        str(args.input),
        "-map",
        "0:v:0",
        "-map",
        "1:a?",
        *_encoder_args(),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-map_metadata",
        "1",
        "-movflags",
        "+faststart",
        str(args.output),
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    completed = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            enhanced = super_resolution.upsample(frame)
            process.stdin.write(enhanced.tobytes())
            completed += 1
            if completed == 1 or completed % 3 == 0 or completed == frame_count:
                percent = (
                    min(100.0, completed / frame_count * 100.0)
                    if frame_count
                    else 0.0
                )
                print(f"VIDEOCLEANER_PROGRESS={percent:.3f}", flush=True)
                print(
                    f"VIDEOCLEANER_STATUS=AI 超清处理中："
                    f"{completed}/{frame_count or '?'} 帧",
                    flush=True,
                )
    except BrokenPipeError:
        pass
    finally:
        capture.release()
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass

    stderr = process.stderr.read().decode("utf-8", errors="replace")
    exit_code = process.wait()
    if exit_code != 0:
        raise RuntimeError(stderr.strip() or f"FFmpeg 退出代码：{exit_code}")
    if not args.output.is_file():
        raise RuntimeError("AI 超清没有生成输出文件。")
    print("VIDEOCLEANER_PROGRESS=100", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
