"""The Tesseract adapter retries a low-confidence read at 90, 180 and 270 degrees.

Pinned (docs/tests/ocr-rotation.md): ``TesseractOcr.read`` runs ``tesseract`` once on the
EXIF-upright image; when that read's mean confidence is below
``ocr.rotation_min_confidence`` (or there were no words) it also runs it on the image rotated by
90, 180 and 270 degrees, in that order and all in the same direction, and returns the candidate
with the highest mean confidence (ties keep the earlier one, a candidate without words never
beats one with words, all without words returns the upright read). ``OcrResult.rotation_degrees``
says which rotation was returned. ``rotation_min_confidence = 0`` turns the retry off. Every run
keeps the ``ocr.timeout_s`` bound.

``subprocess.run`` is faked: the fake finds which corner of the image it was given holds the
black marker square (top-left when upright), so the tests see which rotation each run received
without fixing the direction (clockwise or counter-clockwise is the adapter's choice), and it
answers each run with the next scripted confidence.
"""

import io
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageStat

from paper_trail.adapters.tesseract.ocr import TesseractOcr
from paper_trail.config import OcrSettings
from paper_trail.ports.ocr import OcrResult

TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num"
    "\tleft\ttop\twidth\theight\tconf\ttext\n"
)
UPRIGHT = "top-left"
OPPOSITE = "bottom-right"  # where the marker is after 180 degrees, whatever the direction
SIDEWAYS = {"top-right", "bottom-left"}  # after 90 or 270 degrees


def _marked_photo() -> bytes:
    """A white landscape image with a black square in its top-left corner."""
    image = Image.new("RGB", (1200, 800), (255, 255, 255))
    ImageDraw.Draw(image).rectangle((0, 0, 239, 159), fill=(0, 0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _marked_corner(png: bytes) -> str:
    with Image.open(io.BytesIO(png)) as opened:
        image = opened.convert("L")
    width, height = image.size
    box_w, box_h = width // 6, height // 6
    corners = {
        "top-left": (0, 0),
        "top-right": (width - box_w, 0),
        "bottom-left": (0, height - box_h),
        "bottom-right": (width - box_w, height - box_h),
    }

    def darkness(origin: tuple[int, int]) -> float:
        left, top = origin
        return -ImageStat.Stat(image.crop((left, top, left + box_w, top + box_h))).mean[0]

    return max(corners, key=lambda name: darkness(corners[name]))


@dataclass
class FakeTesseract:
    """Stands for ``subprocess.run``: answers run n with ``confidences[n]`` (None: no words)."""

    confidences: Sequence[float | None]
    corners: list[str] = field(default_factory=list)
    timeouts: list[float | None] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)

    def __call__(
        self, command: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        run = len(self.corners)
        assert run < len(self.confidences), f"unexpected tesseract run #{run + 1}"
        image = kwargs["input"]
        assert isinstance(image, bytes)
        self.corners.append(_marked_corner(image))
        timeout = kwargs.get("timeout")
        self.timeouts.append(timeout if isinstance(timeout, float | int) else None)
        self.commands.append(list(command))
        assert "env" not in kwargs  # the environment is inherited, not rebuilt
        base = Path(command[list(command).index("stdin") + 1])
        confidence = self.confidences[run]
        text = f"CANDIDATE {run}\n" if confidence is not None else "\n"
        rows = TSV_HEADER
        if confidence is not None:
            rows += f"5\t1\t1\t1\t1\t1\t10\t10\t80\t20\t{confidence * 100:.6f}\tCANDIDATE\n"
            rows += f"5\t1\t1\t1\t1\t2\t100\t10\t20\t20\t{confidence * 100:.6f}\t{run}\n"
        else:
            rows += "1\t1\t0\t0\t0\t0\t0\t0\t1200\t800\t-1\t\n"
        base.with_suffix(".txt").write_text(text, encoding="utf-8")
        base.with_suffix(".tsv").write_text(rows, encoding="utf-8")
        return subprocess.CompletedProcess(list(command), 0, b"", b"")


def _read(
    monkeypatch: pytest.MonkeyPatch,
    confidences: Sequence[float | None],
    settings: OcrSettings | None = None,
) -> tuple[FakeTesseract, OcrResult]:
    fake = FakeTesseract(confidences)
    monkeypatch.setattr(subprocess, "run", fake)
    result = TesseractOcr(settings or OcrSettings()).read(_marked_photo())
    return fake, result


def _assert_four_rotations(corners: list[str]) -> None:
    """0, 90, 180, 270 in that order, all turned the same way."""
    assert len(corners) == 4
    assert corners[0] == UPRIGHT
    assert corners[2] == OPPOSITE
    assert {corners[1], corners[3]} == SIDEWAYS


def test_a_confident_upright_read_runs_tesseract_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-shot photo costs exactly one run, as before."""
    fake, result = _read(monkeypatch, [0.85])

    assert fake.corners == [UPRIGHT]
    assert result.text == "CANDIDATE 0\n"
    assert result.mean_confidence == pytest.approx(0.85)
    assert result.rotation_degrees == 0


def test_an_upright_read_at_exactly_the_threshold_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, result = _read(monkeypatch, [0.6], OcrSettings(rotation_min_confidence=0.6))

    assert len(fake.corners) == 1
    assert result.rotation_degrees == 0


@pytest.mark.parametrize(
    "confidences",
    [
        pytest.param([0.41, 0.79, 0.00, 0.30], id="photo-A"),
        pytest.param([0.24, 0.49, 0.28, 0.29], id="photo-B"),
    ],
)
def test_a_low_confidence_read_returns_the_best_rotation(
    monkeypatch: pytest.MonkeyPatch, confidences: list[float]
) -> None:
    """The measured sideways photos: the 90 degree read wins."""
    fake, result = _read(monkeypatch, confidences)

    _assert_four_rotations(fake.corners)
    assert result.text == "CANDIDATE 1\n"
    assert result.mean_confidence == pytest.approx(confidences[1])
    assert result.rotation_degrees == 90


@pytest.mark.parametrize(
    ("confidences", "winner"),
    [
        pytest.param([0.10, 0.20, 0.95, 0.30], 2, id="upside-down"),
        pytest.param([0.10, 0.20, 0.30, 0.90], 3, id="270"),
        pytest.param([0.55, 0.20, 0.30, 0.40], 0, id="upright-still-best"),
    ],
)
def test_the_returned_rotation_names_the_winning_run(
    monkeypatch: pytest.MonkeyPatch, confidences: list[float], winner: int
) -> None:
    fake, result = _read(monkeypatch, confidences)

    _assert_four_rotations(fake.corners)
    assert result.text == f"CANDIDATE {winner}\n"
    assert result.rotation_degrees == 90 * winner


@pytest.mark.parametrize(
    ("confidences", "winner"),
    [
        pytest.param([0.30, 0.50, 0.50, 0.20], 1, id="90-and-180-tie"),
        pytest.param([0.40, 0.40, 0.10, 0.40], 0, id="upright-ties"),
    ],
)
def test_a_tie_keeps_the_earlier_rotation(
    monkeypatch: pytest.MonkeyPatch, confidences: list[float], winner: int
) -> None:
    _, result = _read(monkeypatch, confidences)

    assert result.rotation_degrees == 90 * winner
    assert result.text == f"CANDIDATE {winner}\n"


@pytest.mark.parametrize(
    ("confidences", "winner"),
    [
        pytest.param([None, None, 0.20, None], 2, id="only-180-has-words"),
        pytest.param([0.05, None, None, None], 0, id="only-upright-has-words"),
        pytest.param([None, 0.01, None, 0.02], 3, id="low-but-some-words"),
    ],
)
def test_a_rotation_without_words_never_wins(
    monkeypatch: pytest.MonkeyPatch, confidences: list[float | None], winner: int
) -> None:
    fake, result = _read(monkeypatch, confidences)

    assert len(fake.corners) == 4
    assert result.rotation_degrees == 90 * winner
    assert result.mean_confidence == pytest.approx(confidences[winner])


def test_no_words_at_any_rotation_returns_the_upright_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, result = _read(monkeypatch, [None, None, None, None])

    assert len(fake.corners) == 4
    assert result.mean_confidence is None
    assert result.text.strip() == ""
    assert result.rotation_degrees == 0


@pytest.mark.parametrize("upright", [0.05, None], ids=["low", "no-words"])
def test_a_zero_threshold_turns_rotation_off(
    monkeypatch: pytest.MonkeyPatch, upright: float | None
) -> None:
    fake, result = _read(monkeypatch, [upright], OcrSettings(rotation_min_confidence=0))

    assert fake.corners == [UPRIGHT]
    assert result.rotation_degrees == 0


def test_every_rotation_run_keeps_the_per_run_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = _read(monkeypatch, [0.1, 0.2, 0.3, 0.4], OcrSettings(timeout_s=7.5))

    assert fake.timeouts == [7.5, 7.5, 7.5, 7.5]


def test_tesseract_runs_single_threaded_with_the_rest_of_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OMP_THREAD_LIMIT=1: parallel Tesseract runs (the test workers, the worker and the CLI)
    must not each start a thread per CPU; on a 2-vCPU CI runner that made a run time out. It is
    set through ``env`` on the command line, so the rest of the environment is inherited (no
    module but the CLI reads the environment, S0-AC-14)."""
    fake, _ = _read(monkeypatch, [0.4, 0.3, 0.2, 0.1])

    assert len(fake.commands) == 4
    for command in fake.commands:
        assert command[:3] == ["env", "OMP_THREAD_LIMIT=1", "tesseract"]
