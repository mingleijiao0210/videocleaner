"""Small macOS source-environment check; does not require private models."""

from __future__ import annotations

import platform
import shutil
import sys


def main() -> int:
    print(f"Python: {sys.version.split()[0]}")
    print(f"Machine: {platform.machine()}")
    for name in ("ffmpeg", "ffprobe"):
        path = shutil.which(name)
        if path is None:
            print(f"MISSING: {name} (install it separately or set VIDEOCLEANER_FFMPEG_DIR)")
            return 1
        print(f"{name}: {path}")
    print("Basic macOS source prerequisites are present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
