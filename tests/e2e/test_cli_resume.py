"""``paper-trail process --interrupt-after / --resume / --reprocess`` end to end (AC-C-04,
AC-C-05, S3-AC-08, S3-AC-10; experiment E2's checkpointer step).

Real composition root, SQLite with LangGraph's checkpoints, real Ollama adapters for chat and
embeddings; the model is a scripted fake Ollama on 127.0.0.1. A second CLI invocation is a
second process: it only shares the run's database file.

Pinned (docs/tests/agent-core-C.md):
- ``--interrupt-after NODE`` takes a node of the graph (``agents.graph.NODE_NAMES``); any other
  name is exit 2 listing the valid ones, before anything runs. A paused receipt prints
  ``paused after <node>`` and the hint ``paper-trail process --resume <id>``; exit 0; its trace
  file has ``outcome.status`` ``processing``.
- ``--resume ID`` continues the receipt's thread in this run's database and prints its block;
  the trace file has ``outcome.resumed_from`` (the node the run continued after). It takes no
  PATH, no ``--text-file`` and no ``--reprocess`` (exit 2). Exit 1 for an unknown receipt, one
  with nothing to resume, or one waiting for a review (the message names ``review resolve``);
  exit 4 when the LLM host is down again.
- Exit 4 (LLM host down) prints the hint ``paper-trail process --resume <id>``; a receipt not
  yet saved is PROCESSING/WAITING_FOR_GPU (S3-AC-10).
- ``--reprocess`` re-runs a stored photo's receipt (same id, not "duplicate") from its stored OCR
  text; with ``--text-file`` it is exit 2 (a text is never deduplicated, so never reprocessed).
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.agent_cli import (
    StartOllama,
    configure_cli,
    fake_ollama_env,
    invoke,
    receipt_status,
    rows,
    run_database,
    trace_files,
)
from tests.extractions import COSTCO_OCR, extraction, inconsistent_extraction
from tests.images import PRINTED_RECEIPTS

RUN = "e2"


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
    result = invoke("process", "--no-color", "--run", RUN, *args)
    return result.exit_code, result.output


def _latest_trace(data_dir: Path) -> dict[str, Any]:
    """The run's newest trace file. Two runs of one receipt finishing in the same second share
    a file name, so the newer replaces the older: the newest is always the last run's."""
    newest = max(trace_files(data_dir, RUN), key=lambda path: path.stat().st_mtime_ns)
    document: dict[str, Any] = json.loads(newest.read_text(encoding="utf-8"))
    return document


def _latest_outcome(data_dir: Path) -> dict[str, Any]:
    outcome: dict[str, Any] = _latest_trace(data_dir)["outcome"]
    return outcome


def _stage(data_dir: Path, receipt_id: int = 1) -> str:
    ((stage,),) = rows(
        run_database(data_dir, RUN), "SELECT stage FROM receipts WHERE id = ?", receipt_id
    )
    return str(stage)


# --- AC-C-04 / S3-AC-08 ---------------------------------------------------------------------------


def test_ac_c_04_interrupt_after_extraction_then_resume_without_a_new_extraction(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    first = ollama(extraction())

    code, output = _process("--text-file", str(receipt_text), "--interrupt-after", "extract_text")

    assert code == 0, output
    assert "paused after extract_text" in output
    assert "paper-trail process --resume 1" in output
    assert first.calls == 1
    database = run_database(data_dir, RUN)
    assert receipt_status(database, 1) == "processing"
    assert _latest_outcome(data_dir)["status"] == "processing"

    second = ollama()  # a new process; any chat request would get no scripted reply
    code, output = _process("--resume", "1")

    assert code == 0, output
    assert "COMPLETED" in output
    assert second.calls == 0
    assert second.embed_requests  # matching ran in the resumed process
    assert receipt_status(database, 1) == "completed"
    outcome = _latest_outcome(data_dir)
    assert (outcome["status"], outcome["resumed_from"]) == ("completed", "extract_text")
    assert rows(database, "SELECT match_source FROM line_items ORDER BY position") == [
        ("new",),
        ("new",),
        ("new",),
    ]


def test_an_unknown_breakpoint_is_a_usage_error(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    fake = ollama(extraction())

    code, output = _process("--text-file", str(receipt_text), "--interrupt-after", "lunch")

    assert code == 2, output
    assert "extract_text" in output
    assert fake.calls == 0
    database = run_database(data_dir, RUN)
    if database.exists():  # the run may have been migrated; nothing was processed in it
        assert rows(database, "SELECT count(*) FROM receipts") == [(0,)]


# --- AC-C-05 / S3-AC-10 ---------------------------------------------------------------------------


def test_ac_c_05_the_gpu_drops_then_resume_continues_from_the_last_completed_node(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    """The first attempt is answered, the retry is not (the fake has no reply left and answers
    500): exit 4 with the resume hint. The resumed process asks only for the retry."""
    ollama(inconsistent_extraction())

    code, output = _process("--text-file", str(receipt_text))

    assert code == 4, output
    assert "paper-trail process --resume 1" in output
    database = run_database(data_dir, RUN)
    assert receipt_status(database, 1) == "processing"
    assert _stage(data_dir) == "waiting_for_gpu"

    second = ollama(extraction())
    code, output = _process("--resume", "1")

    assert code == 0, output
    assert second.calls == 1
    assert receipt_status(database, 1) == "completed"
    assert _latest_outcome(data_dir)["resumed_from"] == "supervisor"


def test_resume_with_the_gpu_still_down_is_exit_4(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    ollama(inconsistent_extraction())
    assert _process("--text-file", str(receipt_text))[0] == 4
    ollama()  # still no reply for the retry

    code, output = _process("--resume", "1")

    assert code == 4, output
    assert receipt_status(run_database(data_dir, RUN), 1) == "processing"


# --- what --resume refuses ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [("costco.txt",), ("--text-file", "costco.txt"), ("--reprocess",)],
    ids=["path", "text-file", "reprocess"],
)
def test_resume_takes_nothing_else(
    data_dir: Path,
    ollama: StartOllama,
    receipt_text: Path,
    extra: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(receipt_text.parent)
    fake = ollama(extraction())

    code, output = _process("--resume", "1", *extra)

    assert code == 2, output
    assert "No such option" not in output  # refused by the command, not unknown to the CLI
    assert fake.calls == 0


def test_resume_of_an_unknown_receipt_is_exit_1(data_dir: Path, ollama: StartOllama) -> None:
    ollama()

    code, output = _process("--resume", "42")

    assert code == 1, output
    assert "42" in output


def test_resume_of_a_finished_receipt_is_exit_1(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    ollama(extraction())
    assert _process("--text-file", str(receipt_text))[0] == 0

    code, output = _process("--resume", "1")

    assert code == 1, output
    assert receipt_status(run_database(data_dir, RUN), 1) == "completed"


def test_resume_of_a_receipt_waiting_for_review_points_to_review_resolve(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    ollama(then=inconsistent_extraction())
    assert _process("--text-file", str(receipt_text))[0] == 0

    code, output = _process("--resume", "1")

    assert code == 1, output
    assert "review resolve" in output
    assert receipt_status(run_database(data_dir, RUN), 1) == "needs_review"


# --- --reprocess --------------------------------------------------------------------------------


def test_reprocess_reruns_a_stored_photo_from_its_ocr_text(
    data_dir: Path, ollama: StartOllama, tmp_path: Path
) -> None:
    photo = tmp_path / "costco.png"
    photo.write_bytes(PRINTED_RECEIPTS[0].render())
    fake = ollama(extraction(), extraction())
    assert _process(str(photo))[0] == 0

    code, output = _process(str(photo), "--reprocess")

    assert code == 0, output
    assert "duplicate" not in output.lower()
    assert "receipt 1" in output
    assert fake.calls == 2
    database = run_database(data_dir, RUN)
    assert rows(database, "SELECT count(*) FROM receipts") == [(1,)]
    assert receipt_status(database, 1) == "completed"
    assert rows(database, "SELECT match_source FROM line_items ORDER BY position") == [
        ("alias",),
        ("alias",),
        ("alias",),
    ]
    events = _latest_trace(data_dir)["events"]
    assert [event for event in events if event["name"] == "ocr_read"] == []
    assert events[0]["name"] != "read"


def test_reprocess_with_a_text_file_is_a_usage_error(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    fake = ollama(extraction())

    code, output = _process("--text-file", str(receipt_text), "--reprocess")

    assert code == 2, output
    assert "No such option" not in output
    assert fake.calls == 0
