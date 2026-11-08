"""``paper-trail process`` end to end (AC-A-10, AC-A-11, AC-A-04, AC-A-09, AC-A-06, AC-A-02).

Real composition root, real SQLite, real Ollama adapter, real Tesseract for image inputs; the
model is a scripted fake Ollama on 127.0.0.1 (never the real LLM host).

Pinned (docs/tests/agent-core-A.md):
- ``--run NAME`` uses ``<DATA_DIR>/runs/NAME/paper_trail.db`` (migrated automatically) and
  writes traces to ``<DATA_DIR>/runs/NAME/traces/receipt-<id>-<YYYYMMDDTHHMMSSZ>.json`` and a
  ``summary.json`` beside them; without ``--run`` the main database (which must be migrated,
  else exit 1 with a hint to run ``db upgrade``) and the run name ``main``. NAME matches
  ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``, else exit 2.
- Exit codes: 0 every receipt completed or parked for review; 3 any receipt failed (an
  explicit file that is not an image counts as failed); 4 the LLM host is unreachable or not
  configured (processing stops, the receipt stays PROCESSING with its checkpoint, the message
  names the host); 2 usage errors (no input, PATH together with --text-file, a missing path).
- In a directory, files whose extension is not .jpg/.jpeg/.png/.webp/.heic/.heif (any case)
  are listed as skipped; directories are not recursed; files run in name order.
- Output (``--no-color``): per receipt a header with ``receipt <id>`` and the source name; the
  final line has ``COMPLETED``, ``NEEDS_REVIEW #<review id>`` with
  ``paper-trail review resolve <review id>``, ``FAILED``, or ``duplicate``; ``--verbose`` also
  prints the feedback sent back to the model.
- summary.json: ``run``, ``receipts``, ``completed``, ``needs_review``, ``failed``,
  ``skipped``, ``llm_calls``, ``mean_text_attempts``.
"""

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from paper_trail.config import DATABASE_FILENAME
from tests.e2e.agent_cli import (
    StartOllama,
    chat_contents,
    configure_cli,
    fake_ollama_env,
    invoke,
    receipt_status,
    rows,
    run_database,
    trace_document,
    trace_files,
)
from tests.extractions import (
    COSTCO_OCR,
    GENERIC_RETRY_LINE,
    LINE3_ISSUE,
    MASKED_PAN,
    TEST_PAN_DIGITS,
    extraction,
    inconsistent_extraction,
)
from tests.fakes.ollama_server import closed_loopback_url
from tests.images import PRINTED_RECEIPTS
from tests.pans import card_numbers
from tests.support import CopyMigrated

TRACE_NAME = re.compile(r"receipt-(\d+)-\d{8}T\d{6}Z\.json")


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path)


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartOllama]:
    with fake_ollama_env(monkeypatch) as start:
        yield start


@pytest.fixture
def receipt_text(tmp_path: Path) -> Path:
    path = tmp_path / "costco.txt"
    path.write_text(COSTCO_OCR, encoding="utf-8")
    return path


def _process(*args: str) -> tuple[int, str]:
    result = invoke("process", "--no-color", *args)
    return result.exit_code, result.output


# --- completed and reviewed receipts (exit 0) ------------------------------------------------


def test_ac_a_10_a_clean_receipt_completes_with_exit_0(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    fake = ollama(extraction())

    code, output = _process("--run", "e1", "--text-file", str(receipt_text))

    assert code == 0, output
    assert "receipt 1" in output
    assert "costco.txt" in output
    assert "COMPLETED" in output
    assert receipt_status(run_database(data_dir, "e1"), 1) == "completed"
    assert fake.calls == 1
    body = fake.chat_requests[0]
    assert body["model"] == "qwen2.5:7b-instruct"
    assert body["options"]["num_ctx"] == 8192
    assert body["options"]["temperature"] == 0.0
    assert body["format"]["additionalProperties"] is False


def test_ac_a_10_a_receipt_parked_for_review_is_exit_0_with_the_resolve_hint(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    ollama(then=inconsistent_extraction())

    code, output = _process("--run", "rv", "--text-file", str(receipt_text))

    assert code == 0, output
    assert "NEEDS_REVIEW #1" in output
    assert "paper-trail review resolve 1" in output
    assert receipt_status(run_database(data_dir, "rv"), 1) == "needs_review"


# --- AC-A-11 and AC-A-04: text input, masking, the JSON trace -----------------------------------


def test_ac_a_11_text_file_is_masked_before_anything_else(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    """AC-A-11 / AC-A-04: the card number never reaches the model, the DB or the trace file."""
    fake = ollama(extraction())

    code, output = _process("--run", "e4", "--text-file", str(receipt_text))

    assert code == 0, output
    sent = "\n".join(chat_contents(fake, 0))
    assert MASKED_PAN in sent
    assert card_numbers(sent) == []
    (trace,) = trace_files(data_dir, "e4")
    text = trace.read_text(encoding="utf-8")
    assert card_numbers(text) == []
    assert TEST_PAN_DIGITS not in text.replace(" ", "")
    (ocr_text,) = rows(run_database(data_dir, "e4"), "SELECT ocr_text FROM receipts")[0]
    assert MASKED_PAN in ocr_text
    assert card_numbers(output) == []


def test_ac_a_09_the_json_trace_validates_and_tells_the_story(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    """AC-A-09 / plan 4.3: one trace per receipt, valid paper-trail.trace/v1, totals that
    match the events, LLM events with tag, model, attempt and duration."""
    ollama(inconsistent_extraction(), extraction())

    code, output = _process("--run", "tr", "--text-file", str(receipt_text))

    assert code == 0, output
    (path,) = trace_files(data_dir, "tr")
    assert TRACE_NAME.fullmatch(path.name)
    document = trace_document(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["schema"] == "paper-trail.trace/v1"
    assert raw["run"] == "tr"
    assert raw["receipt_id"] == 1
    assert raw["source_name"] == "costco.txt"
    assert raw["image_sha256"] is None
    assert raw["outcome"]["status"] == "completed"
    assert raw["config"]["feedback"] is True
    assert raw["config"]["examples"] is True
    assert raw["config"]["max_text_attempts"] == 2
    assert raw["config"]["transport"] == "inprocess"
    assert raw["config"]["prompt_versions"]["extraction"] == "v1"
    assert raw["config"]["models"]["text"] == "qwen2.5:7b-instruct"
    events = raw["events"]
    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    llm_events = [event for event in events if event["kind"] == "llm"]
    assert [event["data"]["attempt"] for event in llm_events] == [1, 2]
    assert all(event["name"] == "extraction/v1" for event in llm_events)
    assert all(isinstance(event["duration_ms"], int) for event in llm_events)
    totals = raw["totals"]
    assert totals["llm_calls"] == 2
    assert totals["text_attempts"] == 2
    assert totals["prompt_tokens"] == 240
    assert totals["tool_calls"] == len([event for event in events if event["kind"] == "tool"])
    assert totals["tool_denied"] == 0
    assert document.receipt_id == 1
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # atomic: no temp files left


def test_ac_a_10_the_run_writes_a_summary(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    ollama(inconsistent_extraction(), extraction())

    _process("--run", "sm", "--text-file", str(receipt_text))

    summary = json.loads((data_dir / "runs" / "sm" / "summary.json").read_text("utf-8"))
    assert summary["run"] == "sm"
    assert (summary["receipts"], summary["completed"], summary["needs_review"]) == (1, 1, 0)
    assert (summary["failed"], summary["skipped"]) == (0, 0)
    assert summary["llm_calls"] == 2
    assert summary["mean_text_attempts"] == 2.0


# --- AC-A-06 flags --------------------------------------------------------------------------------


def test_ac_a_06_no_feedback_flag(data_dir: Path, ollama: StartOllama, receipt_text: Path) -> None:
    fake = ollama(inconsistent_extraction(), extraction())

    code, output = _process("--run", "gen", "--no-feedback", "--text-file", str(receipt_text))

    assert code == 0, output
    second = "\n".join(chat_contents(fake, 1))
    assert GENERIC_RETRY_LINE in second
    assert LINE3_ISSUE not in second
    (trace,) = trace_files(data_dir, "gen")
    assert json.loads(trace.read_text("utf-8"))["config"]["feedback"] is False


def test_ac_a_06_max_text_attempts_flag(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    fake = ollama(then=inconsistent_extraction())

    code, output = _process(
        "--run", "one", "--max-text-attempts", "1", "--no-vision", "--text-file", str(receipt_text)
    )

    assert code == 0, output
    assert fake.calls == 1
    assert "NEEDS_REVIEW" in output


def test_ac_a_13_no_examples_flag(data_dir: Path, ollama: StartOllama, receipt_text: Path) -> None:
    fake = ollama(extraction())

    code, output = _process("--run", "noex", "--no-examples", "--text-file", str(receipt_text))

    assert code == 0, output
    roles = [message["role"] for message in fake.chat_requests[0]["messages"]]
    assert roles == ["system", "user"]


def test_verbose_prints_the_feedback(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    ollama(inconsistent_extraction(), extraction())

    code, output = _process("--run", "vb", "--verbose", "--text-file", str(receipt_text))

    assert code == 0, output
    assert LINE3_ISSUE in output


# --- images, directories, duplicates --------------------------------------------------------------


def test_ac_a_10_a_directory_skips_non_images(
    data_dir: Path, ollama: StartOllama, tmp_path: Path
) -> None:
    """AC-A-10: non-image files in a directory are listed as skipped, not fatal."""
    folder = tmp_path / "receipts"
    folder.mkdir()
    (folder / "costco.JPG").write_bytes(PRINTED_RECEIPTS[0].render())
    (folder / "notes.txt").write_text("not a receipt", encoding="utf-8")
    (folder / "README.md").write_text("# receipts", encoding="utf-8")
    (folder / "nested").mkdir()
    fake = ollama(extraction())

    code, output = _process("--run", "dir", str(folder))

    assert code == 0, output
    assert fake.calls == 1
    assert "costco.JPG" in output
    assert re.search(r"skipped.*notes\.txt", output, re.IGNORECASE | re.DOTALL)
    assert re.search(r"skipped.*README\.md", output, re.IGNORECASE | re.DOTALL)
    database = run_database(data_dir, "dir")
    ((sha,),) = rows(database, "SELECT image_sha256 FROM receipts")
    assert sha is not None
    assert list((data_dir / "images").rglob(f"{sha}.jpg"))


def test_ac_a_02_the_same_photo_twice_is_a_duplicate(
    data_dir: Path, ollama: StartOllama, tmp_path: Path
) -> None:
    photo = tmp_path / "costco.jpg"
    photo.write_bytes(PRINTED_RECEIPTS[0].render())
    fake = ollama(extraction())

    first = _process("--run", "dup", str(photo))
    second = _process("--run", "dup", str(photo))

    assert first[0] == second[0] == 0, second[1]
    assert "duplicate" in second[1].lower()
    assert fake.calls == 1
    assert rows(run_database(data_dir, "dup"), "SELECT count(*) FROM receipts") == [(1,)]


def test_ac_a_10_a_file_that_is_not_an_image_fails_with_exit_3(
    data_dir: Path, ollama: StartOllama, tmp_path: Path
) -> None:
    good = tmp_path / "a-costco.jpg"
    good.write_bytes(PRINTED_RECEIPTS[0].render())
    bad = tmp_path / "b-fake.jpg"
    bad.write_bytes(b"%PDF-1.7 a pdf named like a photo")
    fake = ollama(extraction())

    code, output = _process("--run", "bad", str(good), str(bad))

    assert code == 3, output
    assert "b-fake.jpg" in output
    assert "COMPLETED" in output  # the good one still ran
    assert fake.calls == 1


def test_ac_a_10_runs_are_isolated(data_dir: Path, ollama: StartOllama, receipt_text: Path) -> None:
    ollama(then=extraction())

    _process("--run", "a", "--text-file", str(receipt_text))
    _process("--run", "b", "--text-file", str(receipt_text))

    for run in ("a", "b"):
        assert rows(run_database(data_dir, run), "SELECT id FROM receipts") == [(1,)]
    assert not (data_dir / "paper_trail.db").exists()


# --- the LLM host (exit 4) ---------------------------------------------------------------------


def test_ac_a_10_an_unreachable_llm_host_is_exit_4_and_keeps_the_checkpoint(
    data_dir: Path, receipt_text: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-A-10 / S3-AC-10: exit 4 naming the host; the receipt stays PROCESSING and its
    LangGraph thread is checkpointed for a later resume."""
    url = closed_loopback_url()
    monkeypatch.setenv("PAPER_TRAIL_OLLAMA__BASE_URL", url)

    code, output = _process("--run", "down", "--text-file", str(receipt_text))

    assert code == 4, output
    assert url.removeprefix("http://") in output
    database = run_database(data_dir, "down")
    assert receipt_status(database, 1) == "processing"
    threads = rows(database, "SELECT DISTINCT thread_id FROM checkpoints")
    assert threads == [("receipt-1",)]


def test_ac_a_10_an_unconfigured_llm_host_is_exit_4(data_dir: Path, receipt_text: Path) -> None:
    code, output = _process("--run", "none", "--text-file", str(receipt_text))

    assert code == 4, output
    assert "not configured" in output


def test_ac_a_10_the_llm_host_down_stops_the_batch(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_OLLAMA__BASE_URL", closed_loopback_url())
    folder = tmp_path / "receipts"
    folder.mkdir()
    for index, printed in enumerate(PRINTED_RECEIPTS[:2]):
        (folder / f"{index}.png").write_bytes(printed.render())

    code, _ = _process("--run", "stop", str(folder))

    assert code == 4
    assert rows(run_database(data_dir, "stop"), "SELECT count(*) FROM receipts") == [(1,)]


# --- usage errors and the main database ---------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--run", "x", "missing.jpg"),
        ("--run", "../escape", "--text-file", "costco.txt"),
        ("--run", "x", "costco.txt", "--text-file", "costco.txt"),
    ],
    ids=["no-input", "missing-path", "bad-run-name", "path-and-text-file"],
)
def test_ac_a_10_usage_errors_are_exit_2(
    data_dir: Path,
    ollama: StartOllama,
    receipt_text: Path,
    args: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(receipt_text.parent)
    fake = ollama(extraction())

    code, output = _process(*args)

    assert code == 2, output
    assert fake.calls == 0


def test_the_main_database_must_be_migrated(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    fake = ollama(extraction())

    code, output = _process("--text-file", str(receipt_text))

    assert code == 1, output
    assert "db upgrade" in output
    assert fake.calls == 0


def test_the_main_run_uses_the_main_database(
    data_dir: Path, ollama: StartOllama, receipt_text: Path, copy_migrated: CopyMigrated
) -> None:
    copy_migrated(data_dir / DATABASE_FILENAME)
    ollama(extraction())

    code, output = _process("--text-file", str(receipt_text))

    assert code == 0, output
    assert receipt_status(data_dir / "paper_trail.db", 1) == "completed"
    assert len(trace_files(data_dir, "main")) == 1
