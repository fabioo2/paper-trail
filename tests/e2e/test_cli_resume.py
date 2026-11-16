"""``paper-trail process --interrupt-after / --resume / --reprocess`` end to end (AC-C-04,
AC-C-05, S3-AC-08; claude-extraction CE-TC-056, 057, 059, 060).

Real composition root, SQLite with LangGraph's checkpoints, the real Anthropic adapter; the
model is a scripted fake Messages API on 127.0.0.1. A second CLI invocation is a second
process: it only shares the run's database file (and gets a fake API of its own).

Pinned:
- ``--interrupt-after NODE`` takes a node of the graph (the 9 of ``agents.graph.NODE_NAMES``);
  any other name is exit 2 listing the valid ones, before anything runs. A paused receipt
  prints ``paused after <node>`` and the hint ``paper-trail process --resume <id>``; exit 0.
- ``--resume ID`` continues the receipt's thread; the trace has ``outcome.resumed_from``. It
  takes no PATH and no ``--reprocess`` (exit 2). Exit 1 for an unknown receipt, one with
  nothing to resume, or one waiting for a review (the message names ``review resolve``); exit
  2 for a run saved at state version 2 (the message names ``review resolve`` and
  ``process --reprocess``); exit 4 when Claude is unavailable again.
- Claude unavailable: exit 4 with the hint; the receipt is FAILED with its checkpoint (V11).
- ``--reprocess`` re-reads a stored photo's receipt with Claude (same id, not "duplicate").
"""

import copy
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig

from paper_trail.adapters.sqlite.checkpoints import sqlite_checkpointer
from paper_trail.agents.state import thread_id
from tests.e2e.agent_cli import (
    StartClaude,
    configure_cli,
    fake_claude_env,
    invoke,
    photo,
    receipt_status,
    rows,
    run_database,
    trace_files,
)
from tests.extractions import answer, inconsistent_answer, metro_answer
from tests.fakes.anthropic_server import UNAVAILABLE

RUN = "e2"
EXTRACT_MODEL = "claude-haiku-5-5"
RETRY_MODEL = "claude-opus-5-5"
NODE_NAMES = (
    "extract",
    "validate",
    "supervisor",
    "review",
    "persist",
    "match",
    "commit_matches",
    "enrich",
    "commit_enrichment",
)
OLD_STATE_VERSION = 2


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path, main_migrated=True)


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartClaude]:
    with fake_claude_env(monkeypatch) as start:
        yield start


@pytest.fixture
def receipt(tmp_path: Path) -> Path:
    return photo(tmp_path)


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


def _status_and_stage(data_dir: Path) -> list[tuple[Any, ...]]:
    return rows(run_database(data_dir, RUN), "SELECT status, stage FROM receipts")


# --- AC-C-04 / S3-AC-08 / CE-TC-059: breakpoints --------------------------------------------------


def test_ac_c_04_interrupt_after_extract_then_resume_without_a_new_read(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    first = claude(answer())

    code, output = _process(str(receipt), "--interrupt-after", "extract")

    assert code == 0, output
    assert "paused after extract" in output
    assert "paper-trail process --resume 1" in output
    assert first.calls == 1
    database = run_database(data_dir, RUN)
    assert receipt_status(database, 1) == "processing"
    assert _latest_outcome(data_dir)["status"] == "processing"

    second = claude()  # a new process; any request would get no scripted reply
    code, output = _process("--resume", "1")

    assert code == 0, output
    assert "COMPLETED" in output
    assert second.calls == 0
    assert receipt_status(database, 1) == "completed"
    outcome = _latest_outcome(data_dir)
    assert (outcome["status"], outcome["resumed_from"]) == ("completed", "extract")
    assert rows(database, "SELECT match_source FROM line_items ORDER BY position") == [
        ("new",),
        ("new",),
        ("new",),
    ]


@pytest.mark.parametrize("node", ["read", "extract_text", "extract_vision", "lunch"])
def test_ce_tc_059_an_unknown_breakpoint_is_a_usage_error_listing_the_nine_nodes(
    data_dir: Path, claude: StartClaude, receipt: Path, node: str
) -> None:
    fake = claude(answer())

    code, output = _process(str(receipt), "--interrupt-after", node)

    assert code == 2, output
    for name in NODE_NAMES:
        assert name in output, name
    assert fake.calls == 0
    database = run_database(data_dir, RUN)
    if database.exists():  # the run may have been migrated; nothing was processed in it
        assert rows(database, "SELECT count(*) FROM receipts") == [(0,)]


# --- AC-C-05 / CE-TC-056: Claude unavailable, then resume -----------------------------------------


def test_ce_tc_056_claude_down_on_the_first_read_then_resume_completes(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    claude(UNAVAILABLE)

    code, output = _process(str(receipt))

    assert code == 4, output
    assert f"paper-trail process --resume 1 --run {RUN}" in output
    assert _status_and_stage(data_dir) == [("failed", "failed")]
    database = run_database(data_dir, RUN)
    assert rows(database, "SELECT DISTINCT thread_id FROM checkpoints") == [("receipt-1",)]

    second = claude(answer())
    code, output = _process("--resume", "1")

    assert code == 0, output
    assert second.models == [EXTRACT_MODEL]
    assert _status_and_stage(data_dir) == [("completed", "done")]
    assert rows(database, "SELECT count(*) FROM receipts") == [(1,)]


def test_ce_tc_056_resume_with_claude_still_down_is_exit_4_and_still_resumable(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    claude(UNAVAILABLE)
    assert _process(str(receipt))[0] == 4
    claude(UNAVAILABLE)

    code, output = _process("--resume", "1")

    assert code == 4, output
    assert "paper-trail process --resume 1" in output
    assert _status_and_stage(data_dir) == [("failed", "failed")]

    claude(answer())
    assert _process("--resume", "1")[0] == 0


def test_a_failed_receipt_processed_again_without_resume_is_a_duplicate(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    """The way back for a FAILED receipt is ``--resume`` (or ``--reprocess``): the same photo
    again is the receipt it already made, and nothing is read."""
    claude(UNAVAILABLE)
    assert _process(str(receipt))[0] == 4
    fake = claude(answer())

    code, output = _process(str(receipt))

    assert code == 0, output
    assert "duplicate" in output.lower()
    assert fake.calls == 0
    assert _status_and_stage(data_dir) == [("failed", "failed")]


# --- what --resume refuses ----------------------------------------------------------------------


@pytest.mark.parametrize("extra", [("costco.png",), ("--reprocess",)], ids=["path", "reprocess"])
def test_resume_takes_nothing_else(
    data_dir: Path,
    claude: StartClaude,
    receipt: Path,
    extra: tuple[str, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(receipt.parent)
    fake = claude(answer())

    code, output = _process("--resume", "1", *extra)

    assert code == 2, output
    assert "No such option" not in output  # refused by the command, not unknown to the CLI
    assert fake.calls == 0


def test_resume_of_an_unknown_receipt_is_exit_1(data_dir: Path, claude: StartClaude) -> None:
    claude()

    code, output = _process("--resume", "42")

    assert code == 1, output
    assert "42" in output


def test_resume_of_a_finished_receipt_is_exit_1(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    claude(answer())
    assert _process(str(receipt))[0] == 0

    code, output = _process("--resume", "1")

    assert code == 1, output
    assert receipt_status(run_database(data_dir, RUN), 1) == "completed"


def test_resume_of_a_receipt_waiting_for_review_points_to_review_resolve(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    claude(then=inconsistent_answer())
    assert _process(str(receipt))[0] == 0

    code, output = _process("--resume", "1")

    assert code == 1, output
    assert "review resolve" in output
    assert receipt_status(run_database(data_dir, RUN), 1) == "needs_review"


# --- CE-TC-057: a run saved at state version 2 ------------------------------------------------


def _save_as_version_2(database: Path, receipt_id: int) -> None:
    """Rewrite the receipt's latest checkpoint as the version before Claude saved it
    (``state_version`` 2): the OCR/Ollama graph's state."""
    config: RunnableConfig = {
        "configurable": {"thread_id": thread_id(receipt_id), "checkpoint_ns": ""}
    }
    with sqlite_checkpointer(database) as saver:
        saved = saver.get_tuple(config)
        assert saved is not None
        checkpoint = copy.copy(saved.checkpoint)
        versions = dict(checkpoint["channel_versions"])
        values = dict(checkpoint["channel_values"])
        values["state_version"] = OLD_STATE_VERSION
        versions["state_version"] = max(versions.values())
        checkpoint["channel_values"] = values
        checkpoint["channel_versions"] = versions
        saver.put(saved.parent_config or config, checkpoint, saved.metadata, versions)


def test_ce_tc_057_resume_of_a_version_2_run_is_exit_2_naming_both_ways_out(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    claude(answer())
    assert _process(str(receipt), "--interrupt-after", "extract")[0] == 0
    database = run_database(data_dir, RUN)
    _save_as_version_2(database, 1)
    before = rows(database, "SELECT status, stage, total_cents FROM receipts")
    fake = claude()

    code, output = _process("--resume", "1")

    assert code == 2, output
    assert "review resolve" in output
    assert "--reprocess" in output
    assert fake.calls == 0
    assert rows(database, "SELECT status, stage, total_cents FROM receipts") == before
    assert rows(database, "SELECT count(*) FROM line_items") == [(0,)]
    assert rows(database, "SELECT count(*) FROM products") == [(0,)]


# --- CE-TC-060: --reprocess re-reads the stored photo -------------------------------------------


def test_ce_tc_060_reprocess_rereads_the_stored_photo_with_claude(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    fake = claude(answer(), metro_answer())
    assert _process(str(receipt))[0] == 0
    database = run_database(data_dir, RUN)
    with_ocr = "UPDATE receipts SET ocr_text = 'OLD OCR TEXT' WHERE id = 1"
    _execute(database, with_ocr)

    code, output = _process(str(receipt), "--reprocess")

    assert code == 0, output
    assert "duplicate" not in output.lower()
    assert "receipt 1" in output
    assert fake.calls == 2
    assert fake.image_sha256(1) == fake.image_sha256(0)
    assert rows(database, "SELECT id, ocr_text FROM receipts") == [(1, "OLD OCR TEXT")]
    assert receipt_status(database, 1) == "completed"
    assert rows(database, "SELECT count(*), sum(line_total_cents) FROM line_items") == [(12, 4204)]


def test_r8_reprocess_with_claude_down_keeps_the_receipt_as_it_was(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    """Review round 1, R8: a reprocess that stops before ``persist`` restores the receipt's
    status and stage; its lines are untouched."""
    claude(answer())
    assert _process(str(receipt))[0] == 0
    database = run_database(data_dir, RUN)
    lines = rows(database, "SELECT position, raw_text, line_total_cents FROM line_items")
    fake = claude(UNAVAILABLE)

    code, output = _process(str(receipt), "--reprocess")

    assert code == 4, output
    assert fake.calls >= 1
    assert _status_and_stage(data_dir) == [("completed", "done")]
    assert rows(database, "SELECT position, raw_text, line_total_cents FROM line_items") == lines


def test_ce_tc_060_reprocess_is_the_way_out_of_a_version_2_run(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    claude(answer())
    assert _process(str(receipt), "--interrupt-after", "extract")[0] == 0
    database = run_database(data_dir, RUN)
    _save_as_version_2(database, 1)
    fake = claude(answer())

    code, output = _process(str(receipt), "--reprocess")

    assert code == 0, output
    assert fake.models == [EXTRACT_MODEL]
    assert receipt_status(database, 1) == "completed"
    assert rows(database, "SELECT count(*) FROM receipts") == [(1,)]


def test_ce_tc_060_a_receipt_without_a_stored_image_cannot_be_reprocessed(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    claude(answer())
    assert _process(str(receipt))[0] == 0
    database = run_database(data_dir, RUN)
    _execute(database, "UPDATE receipts SET image_path = NULL WHERE id = 1")
    fake = claude(answer())

    code, output = _process(str(receipt), "--reprocess")

    assert code == 1, output
    assert fake.calls == 0


def _execute(database: Path, sql: str) -> None:
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(sql)
