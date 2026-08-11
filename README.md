# VideoCleaner

Offline Windows and macOS desktop application for removing fixed text overlays, subtitles and watermarks from video, with optional local 2× clarity enhancement.

The project processes media locally. It does not require an account, upload video, or call a cloud AI API.

## Scope of this repository

This repository contains the application source, tests and the small Apache-2.0 FSRCNN 2× enhancement asset. Large runtime components are deliberately not committed:

- FFmpeg binaries must be installed separately and must retain their own license.
- AI/VSR model weights and runtimes are optional downloads; no model weights are included here.
- The non-commercial ProPainter component is not included in this public source snapshot.
- Real videos, screenshots, logs, local settings, caches and build artifacts are excluded.

The basic FFmpeg modes and the clarity-enhancement workflow can be developed from this repository. The optional VSR/AI modes require a separately obtained, license-compatible engine.

## Features

- Visual multi-region selection with coordinate display.
- Offline FFmpeg processing with audio preservation when supported.
- Fast interpolation, soft blending and local smart-mask workflows.
- Optional fast clarity enhancement and offline FSRCNN 2× upscaling.
- Progress, elapsed-time display, cancellation and cleanup.
- Windows and Apple Silicon macOS path detection without hard-coded personal directories.

## System requirements

### Windows version

- Windows 10 22H2 or Windows 11, 64-bit.
- x86-64 CPU with at least 4 physical cores; 8 cores recommended for long videos.
- 8 GB RAM minimum; 16 GB recommended for 1080p work; 32 GB for optional AI/VSR workloads.
- At least 10 GB free disk space for temporary files and output; more for 4K video.
- FFmpeg and ffprobe 6.x or newer available on `PATH` or configured with `VIDEOCLEANER_FFMPEG_DIR`.
- Python 3.11 or 3.12 for source development.

### macOS version

- macOS 13 Ventura or newer.
- Apple Silicon (M1/M2/M3/M4) recommended; M2 Pro with 32 GB unified memory is the reference configuration for local AI workloads.
- 16 GB unified memory minimum for ordinary processing; 32 GB recommended for high-resolution or optional AI/VSR workloads.
- At least 10 GB free disk space for temporary files and output.
- Native arm64 FFmpeg and ffprobe available on `PATH` or configured with `VIDEOCLEANER_FFMPEG_DIR`.
- Python 3.11 or 3.12 for source development.

These are practical recommendations, not a claim that every mode has been benchmarked on every machine. Processing time varies with resolution, frame rate, selection size and number of regions.

## Installation from source

### Windows

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
python start.py
```

Install FFmpeg separately from an official or trusted distributor and verify:

```powershell
ffmpeg -version
ffprobe -version
```

If FFmpeg is not on `PATH`, set `VIDEOCLEANER_FFMPEG_DIR` to the directory containing `ffmpeg.exe` and `ffprobe.exe`.

### macOS

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-macos.txt
python start.py
```

The heavy VSR/AI dependencies are intentionally separate:
`python -m pip install -r requirements-optional-vsr.txt`. Install them only
after confirming that the separately obtained engine and models may be used
and redistributed in your situation.

For Apple Silicon, use an arm64 Python and arm64 FFmpeg. If FFmpeg is not on `PATH`, set `VIDEOCLEANER_FFMPEG_DIR` to the directory containing `ffmpeg` and `ffprobe`.

## Usage

1. Choose a video.
2. Draw one or more rectangles around fixed text or watermarks.
3. Choose a processing mode and, if needed, a time range.
4. Choose an output path and start processing.
5. Review the output; the source video is never overwritten.

The enhancement-only option can run without a text selection. FSRCNN 2× enlarges width and height and is capped at 4K output; it cannot reconstruct details that were never recorded.

## Configuration

The application stores user preferences in the platform-specific application data directory. Do not commit local settings. Optional runtime paths can be supplied with environment variables:

- `VIDEOCLEANER_FFMPEG_DIR`: directory containing `ffmpeg` and `ffprobe`.
- `VIDEOCLEANER_VSR_ROOT`: separately installed VSR engine root, if you have a license-compatible engine.
- `VIDEOCLEANER_DATA_DIR`: optional writable directory for logs, preferences and default output.

See [`.env.example`](.env.example) for the variable names. Never put real tokens, credentials or private paths in committed files.

## Testing

```bash
python -m pytest -q -m "not integration"
```

Integration tests require local FFmpeg and synthetic test media. No personal videos are required or included.

## Known limitations

- Fixed-position text is supported best; moving objects need manual tracking or repeated selections.
- Large selections and backgrounds fully hidden behind text can leave visible reconstruction artifacts.
- Optional VSR/AI modes are not bundled in this repository and depend on separately licensed runtimes and model weights.
- Hardware acceleration is platform and encoder dependent.

## Roadmap

- Publish a license-compatible optional VSR adapter with provenance and reproducible setup.
- Add automated Windows and macOS CI smoke tests using synthetic media only.
- Provide signed release artifacts after dependency and redistribution review.
- Improve moving-text tracking and mask quality.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please do not submit real user media, credentials, logs or proprietary model files.

## Security

Please report vulnerabilities privately according to [SECURITY.md](SECURITY.md). Do not open a public issue containing a secret.

## License

Original application code is provided under the [Apache License 2.0](LICENSE).
Third-party components retain their own licenses; see
[NOTICE](NOTICE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). The
optional ProPainter component is intentionally excluded because its official
license is non-commercial.
