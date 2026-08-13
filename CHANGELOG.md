# Changelog

## 1.8.2 stable A

- Published the locally validated stable A processing core.
- Preserved the automatic safety margin around manual selections.
- Kept the color-agnostic, multilingual text detection and real-background
  blending pipeline.

## 1.8.1

- Improved language-agnostic text masks for colored, outlined and gradient glyphs.
- Added LAB/HSV local contrast and multi-channel edge detection for green,
  cyan, blue, purple and other colored text.
- Lowered temporal voting strictness for animated and fading text.
- Added multilingual and multicolor regression coverage.

## 1.8.2 candidate

- Increased colored-text outline margins for isolated residual outlines.
- The previous baseline is preserved as the `v1.8.1-stable` tag.

## 1.8.4 candidate

- Based on the fast 1.8.2 baseline.
- Added optional local repair only for small dark residual components on nearly
  uniform backgrounds; no full-crop second inpaint pass.
- The 1.8.2 baseline is preserved as `v1.8.2-stable`.

## Unreleased

- Prepared a privacy-filtered Windows/macOS source layout.
- Added configurable FFmpeg and optional VSR paths.
- Documented platform requirements and third-party redistribution limits.

## 1.8.0

- Added fast clarity enhancement and offline FSRCNN 2× upscaling.
- Added elapsed-time reporting and cancellation cleanup.
