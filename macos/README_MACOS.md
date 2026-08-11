# macOS setup

This source tree targets macOS 13 or newer and is tested conceptually for
Apple Silicon. It does not include a signed `.app`, FFmpeg binaries, private
models or a personal build environment.

## Recommended hardware

- Apple Silicon M1 or newer.
- 16 GB unified memory for ordinary processing.
- 32 GB unified memory for high-resolution or optional AI/VSR workloads.
- At least 10 GB free disk space for temporary frames and output.

## Run from source

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-macos.txt
python start.py
```

Install an arm64 FFmpeg and verify:

```bash
ffmpeg -version
ffprobe -version
```

If the binaries are installed elsewhere, set:

```bash
export VIDEOCLEANER_FFMPEG_DIR="/path/to/ffmpeg/bin"
```

The optional VSR engine can be supplied separately with
`VIDEOCLEANER_VSR_ROOT`. It is intentionally not bundled until every model
and code license has been verified.

## Building a signed application

Packaging is not provided by this staging snapshot. A future release should
build on a real Apple Silicon Mac, preserve FFmpeg and model notices, and be
signed/notarized by the maintainer. Do not claim that a DMG is tested until a
real build has passed the smoke tests.
