# OCR rotation retry: acceptance criteria to tests

Written by the test author on 2026-11-08, before the implementation. Problem: phone photos stored landscape with the receipt lying sideways (EXIF orientation already applied) give Tesseract `--psm 4` garbage, and OSD (`--psm 0`) refuses them ("Too few characters"). Measured mean confidences per rotation (0/90/180/270): photo A 0.41/0.79/0.00/0.30, photo B 0.24/0.49/0.28/0.29; the synthetic receipts of `tests/images.py` turned 90 degrees give no word at all upright.

Pinned: `OcrSettings.rotation_min_confidence: float = 0.6` (0..1, `PAPER_TRAIL_OCR__ROTATION_MIN_CONFIDENCE`); `OcrResult.rotation_degrees: int = 0`; the `ocr_read` tool output gains `rotation_degrees` next to `mean_confidence`, so the trace's tool output carries it and the `ocr_read` summary says `rotated N°` when N is not 0. Rotations run in the order 0, 90, 180, 270, all in one direction (clockwise or counter-clockwise: the adapter's choice, documented in its docstring). The adapter unit tests fake `subprocess.run`; the fake detects the rotation each run received from a marker in the image, so they do not fix the direction.

| Criterion | Tests |
|---|---|
| R-1 Upright read at or above the threshold: exactly one Tesseract run, `rotation_degrees == 0` | `tests/unit/adapters/test_tesseract_rotation.py::test_a_confident_upright_read_runs_tesseract_once`, `::test_an_upright_read_at_exactly_the_threshold_is_kept`; `tests/integration/test_tesseract_ocr.py::test_an_upright_receipt_is_read_without_rotation` |
| R-2 Below the threshold: runs at 0, 90, 180, 270 (same direction) and returns the most confident; `rotation_degrees` names the winner | `test_tesseract_rotation.py::test_a_low_confidence_read_returns_the_best_rotation` (photo A, photo B), `::test_the_returned_rotation_names_the_winning_run` |
| R-3 Ties keep the earlier candidate (0 first) | `test_tesseract_rotation.py::test_a_tie_keeps_the_earlier_rotation` |
| R-4 A candidate without words never beats one with words; all without words returns the upright read | `test_tesseract_rotation.py::test_a_rotation_without_words_never_wins`, `::test_no_words_at_any_rotation_returns_the_upright_read` |
| R-5 `rotation_min_confidence = 0` turns the retry off (one run, even with no words) | `test_tesseract_rotation.py::test_a_zero_threshold_turns_rotation_off` |
| R-6 Every run keeps the per-run `ocr.timeout_s` | `test_tesseract_rotation.py::test_every_rotation_run_keeps_the_per_run_timeout` |
| R-7 Setting: default 0.6, read from the environment, bounded 0..1 | `tests/unit/test_config_ocr_rotation.py` (4 tests) |
| R-8 `ocr_read` returns `rotation_degrees`, over both transports | `tests/unit/tools/test_handlers.py::test_ocr_read_returns_the_rotation_the_ocr_chose`, `::test_ocr_read_of_an_upright_photo_reports_no_rotation`; `tests/contract/test_tool_gateway.py::test_an_allowed_call_returns_json` (key set), `::test_ocr_read_reports_the_rotation_over_every_transport` |
| R-9 The trace shows the rotation (tool output and summary) | `tests/unit/agents/test_tracing.py::test_the_ocr_event_shows_the_chosen_rotation`, `::test_an_upright_ocr_event_does_not_mention_rotation` |
| R-10 Real Tesseract reads a receipt turned 90 degrees upright | `tests/integration/test_tesseract_ocr.py::test_a_sideways_receipt_is_read_upright` |

Test support: `tests/fakes/ocr.py` `FakeOcr.rotation_degrees` (set on `harness.ocr`).
