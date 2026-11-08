"""``paper-trail process`` with colour on, the default (regression, 2026-11-08).

Every other ``process`` suite runs with ``--no-color``, so a style call that only fails when
colour is on went unnoticed: the receipt header asked for the colour ``"bold"`` and Typer
raised ``TypeError: Unknown color 'bold'`` after the receipt was processed, so neither the
trace block nor the run summary was printed or written.

Pinned: with colour on, ``process`` exits as it would with ``--no-color``; the header is bold,
a completed result is green, a review result yellow, a skipped file grey and a failure red, and
the run summary is still printed. This covers the bold header (the call that crashed) and each
colour the command uses; the paused and stopped lines share the yellow call of the review line.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from paper_trail.interfaces.cli.main import app
from tests.e2e.agent_cli import StartOllama, configure_cli, fake_ollama_env
from tests.extractions import COSTCO_OCR, extraction, inconsistent_extraction

BOLD = "\x1b[1m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
GREY = "\x1b[90m"  # bright_black

runner = CliRunner()


@pytest.fixture(autouse=True)
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path)


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartOllama]:
    with fake_ollama_env(monkeypatch) as start:
        yield start


def _process_in_colour(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["process", "--run", "colour", *args], color=True)
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, result.output


def _line_with(output: str, text: str) -> str:
    (line,) = [line for line in output.splitlines() if text in line]
    return line


@pytest.mark.parametrize(
    ("reply", "result", "colour"),
    [
        pytest.param(extraction(), "✔ COMPLETED", GREEN, id="completed"),
        pytest.param(inconsistent_extraction(), "⏸ NEEDS_REVIEW", YELLOW, id="needs-review"),
    ],
)
def test_a_processed_receipt_prints_its_block_in_colour(
    ollama: StartOllama, tmp_path: Path, reply: object, result: str, colour: str
) -> None:
    ollama(then=reply)
    text = tmp_path / "costco.txt"
    text.write_text(COSTCO_OCR, encoding="utf-8")

    code, output = _process_in_colour("--text-file", str(text))

    assert code == 0, output
    assert BOLD in _line_with(output, "receipt 1 · costco.txt")
    assert colour in _line_with(output, result)
    assert "run: 1 receipts" in output


def test_a_skipped_file_and_a_failure_print_in_colour(ollama: StartOllama, tmp_path: Path) -> None:
    ollama()
    folder = tmp_path / "inbox"
    folder.mkdir()
    (folder / "notes.md").write_text("not a receipt", encoding="utf-8")
    (folder / "broken.jpg").write_bytes(b"%PDF-1.7 not an image")

    code, output = _process_in_colour(str(folder))

    assert code == 3, output
    assert GREY in _line_with(output, "skipped notes.md")
    assert RED in _line_with(output, "FAILED broken.jpg")
    assert "1 failed · 1 skipped" in output
