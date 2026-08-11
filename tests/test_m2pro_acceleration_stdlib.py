from __future__ import annotations

import unittest
from pathlib import Path

from app.ai_pipeline_policy import (
    worker_time_range_args,
    worker_video_is_final_encode,
)
from tools.vsr.bridge.videocleaner_vsr_worker import (
    _ffmpeg_writer_command,
    _frame_in_time_range,
    _FramePrefetcher,
)


class _FakeCapture:
    def __init__(self, frames):
        self._frames = iter(frames)

    def read(self):
        try:
            return True, next(self._frames)
        except StopIteration:
            return False, None


class M2ProAccelerationTests(unittest.TestCase):
    def test_worker_range_args_are_stable_and_optional(self):
        self.assertEqual(worker_time_range_args(None, None), [])
        self.assertEqual(
            worker_time_range_args(1.25, 4.5),
            [
                "--range-start",
                "1.250000",
                "--range-end",
                "4.500000",
            ],
        )
        with self.assertRaises(ValueError):
            worker_time_range_args(1.25, None)

    def test_only_strong_mode_can_skip_the_finalize_encode(self):
        self.assertTrue(worker_video_is_final_encode("ai_strong"))
        for mode in ("ai_fast", "ai_precise", "ai_full", "ai_propainter"):
            self.assertFalse(worker_video_is_final_encode(mode))

    def test_frame_time_range_matches_inclusive_ffmpeg_between(self):
        fps = 25.0
        self.assertFalse(_frame_in_time_range(25, fps, 1.0, 2.0))
        self.assertTrue(_frame_in_time_range(26, fps, 1.0, 2.0))
        self.assertTrue(_frame_in_time_range(51, fps, 1.0, 2.0))
        self.assertFalse(_frame_in_time_range(52, fps, 1.0, 2.0))
        self.assertTrue(_frame_in_time_range(999, fps, None, None))

    def test_prefetcher_preserves_frame_order_and_end_marker(self):
        reader = _FramePrefetcher(_FakeCapture(["a", "b", "c"]), buffer_size=2)
        try:
            self.assertEqual(reader.read(), (True, "a"))
            self.assertEqual(reader.read(), (True, "b"))
            self.assertEqual(reader.read(), (True, "c"))
            self.assertEqual(reader.read(), (False, None))
        finally:
            reader.stop()

    def test_macos_writer_uses_one_videotoolbox_encode(self):
        command = _ffmpeg_writer_command(
            Path("/tool/ffmpeg"),
            Path("/tmp/result.mp4"),
            25.0,
            (1920, 1080),
            platform_name="darwin",
        )
        self.assertEqual(command[command.index("-c:v") + 1], "h264_videotoolbox")
        self.assertEqual(command[command.index("-s:v") + 1], "1920x1080")
        self.assertEqual(command.count("-c:v"), 1)
        self.assertIn("+faststart", command)


if __name__ == "__main__":
    unittest.main()
