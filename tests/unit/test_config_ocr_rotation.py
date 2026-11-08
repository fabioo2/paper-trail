"""``ocr.rotation_min_confidence``: below it the OCR also reads the photo turned sideways.

Pinned (docs/tests/ocr-rotation.md, docs/PLAN.md 1.3): ``OcrSettings.rotation_min_confidence``,
a fraction in 0..1, default 0.6, env ``PAPER_TRAIL_OCR__ROTATION_MIN_CONFIDENCE``; 0 turns the
rotation retry off. ``OcrSettings.worst_case_s`` is the longest one ``ocr_read`` may take
(every rotation run at ``timeout_s``), and ``Settings`` refuses an ``MCP__TIMEOUT_S`` that is not
above it; ``OCR__TIMEOUT_S`` defaults to 30 s so the 240 s MCP default covers four runs.
"""

import pytest
from pydantic import ValidationError

from paper_trail.config import OcrSettings, Settings


def test_rotation_min_confidence_defaults_to_0_6() -> None:
    assert Settings().ocr.rotation_min_confidence == pytest.approx(0.6)


def test_rotation_min_confidence_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRAIL_OCR__ROTATION_MIN_CONFIDENCE", "0.75")

    assert Settings().ocr.rotation_min_confidence == pytest.approx(0.75)


@pytest.mark.parametrize("value", [0.0, 1.0])
def test_rotation_min_confidence_accepts_the_bounds(value: float) -> None:
    assert OcrSettings(rotation_min_confidence=value).rotation_min_confidence == value


@pytest.mark.parametrize("value", ["-0.1", "1.01"])
def test_rotation_min_confidence_outside_0_to_1_is_rejected(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_OCR__ROTATION_MIN_CONFIDENCE", value)

    with pytest.raises(ValidationError, match="rotation_min_confidence"):
        Settings()


# --- review fix round 1: the MCP timeout covers the rotation worst case -----------------------


def test_the_ocr_worst_case_counts_every_rotation_run() -> None:
    """Upright plus the 90, 180 and 270 degree reads: four runs, each up to ``timeout_s``."""
    assert OcrSettings(timeout_s=30.0).worst_case_s == pytest.approx(120.0)


def test_without_rotation_the_ocr_worst_case_is_one_run() -> None:
    assert OcrSettings(timeout_s=30.0, rotation_min_confidence=0).worst_case_s == 30.0


def test_the_default_ocr_timeout_is_30_seconds() -> None:
    assert Settings().ocr.timeout_s == 30.0


def test_an_mcp_timeout_below_the_rotation_worst_case_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """60 s per run, four runs: 240 s of OCR, so an MCP call of 240 s could give up early."""
    monkeypatch.setenv("PAPER_TRAIL_OCR__TIMEOUT_S", "60")
    monkeypatch.setenv("PAPER_TRAIL_MCP__TIMEOUT_S", "240")

    with pytest.raises(ValidationError, match="MCP__TIMEOUT_S"):
        Settings()


def test_with_rotation_off_one_ocr_run_bounds_the_mcp_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_OCR__TIMEOUT_S", "60")
    monkeypatch.setenv("PAPER_TRAIL_OCR__ROTATION_MIN_CONFIDENCE", "0")
    monkeypatch.setenv("PAPER_TRAIL_MCP__TIMEOUT_S", "240")

    assert Settings().mcp.timeout_s == 240.0
