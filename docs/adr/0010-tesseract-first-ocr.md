# 10. Tesseract-first OCR with vision-model fallback

Date: 2026-11-06

## Status

Accepted

## Context

OCR must work when the GPU PC is off, and vision-model calls are slow on a 7B VL model. Most receipts are printed and legible.

## Decision

Tesseract (installed in the runtime image, `--psm 4`, Pillow preprocessing: EXIF transpose, grayscale, autocontrast, upscale small images) is the default OCR and runs on the CPU in the worker regardless of GPU state. The vision model re-reads the image only after validation fails twice on Tesseract text (cap from settings).

## Consequences

- Upload to OCR text works with the GPU off (Stage 2 milestone).
- Damaged or faded receipts cost one extra vision call, not every receipt.
- Image size adds roughly 40 MB (Tesseract + English data) to the runtime image.

## Amendment (2026-11-06): agent core slice A

Tesseract 5 with English data (`--no-install-recommends`) adds about 100 MB to the runtime image, not 40 MB: `libtesseract` links ICU, cairo and pango. The runtime image budget is the 500 MB the plan set for this stage, measured as the sum of the image's layers. The adapter runs `tesseract` once per image, writing the text and the word confidences (TSV) together, bounded by `PAPER_TRAIL_OCR__TIMEOUT_S`.

## Amendment (2026-11-08): confidence-based rotation fallback

Phone photos are often stored landscape with the receipt lying sideways (EXIF orientation already applied), and Tesseract's orientation detection (`--psm 0`, OSD) refuses them ("Too few characters"), so it cannot be used to turn them upright. Mean word confidence separates the readings clearly instead: measured on two real photos at 0/90/180/270 degrees, photo A gave 0.41/0.79/0.00/0.30 and photo B 0.24/0.49/0.28/0.29. The adapter therefore reads the prepared image upright first and, only when that read's mean confidence is below `PAPER_TRAIL_OCR__ROTATION_MIN_CONFIDENCE` (default 0.6; 0 turns this off) or it found no words, also reads it turned 90, 180 and 270 degrees counter-clockwise and keeps the most confident read (ties keep the earlier, a read without words never wins). `OcrResult.rotation_degrees` records the winner and the `ocr_read` tool and its trace summary report it. A well-shot receipt still costs one Tesseract run; a low-confidence one costs four, each bounded by `PAPER_TRAIL_OCR__TIMEOUT_S`, so one `ocr_read` call can take up to four times that timeout (`OcrSettings.worst_case_s`). `Settings` therefore requires `PAPER_TRAIL_MCP__TIMEOUT_S` to exceed that worst case, and the per-run default drops from 60 s to 30 s so the 240 s MCP default still covers four runs.

## Amendment (2026-11-08): psm 6 and vision first (ADR-0017)

On the two real crumpled photos, `--psm 4` (one column of variable-size text) dropped the right-hand price column; `--psm 6` (one uniform block of text) recovered it, so `PAPER_TRAIL_OCR__PSM` now defaults to 6. When the OCR read of a photo stays poor (mean confidence below `PAPER_TRAIL_GRAPH__VISION_FIRST_BELOW`, default 0.65, between the measured good read 0.79 and the bad reads 0.41-0.49), the vision model reads the photo first and no text attempt is made: the fallback is no longer only "after validation fails twice". See ADR-0017.
