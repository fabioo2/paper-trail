"""Shared helpers for the agent-core CLI suites (``process``, ``review``, ``graph``).

The CLI runs in-process through Typer's runner with ``PAPER_TRAIL_*`` set by the test, and with
``PAPER_TRAIL_OLLAMA__BASE_URL`` pointing at a scripted fake Ollama on 127.0.0.1; the real
composition root and Ollama adapter run, only the model is a script.
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from paper_trail.adapters.trace.json_file import TraceDocument
from paper_trail.interfaces.cli.main import app
from tests.fakes.ollama_server import FakeOllama, serve_fake_ollama

type StartOllama = Callable[..., FakeOllama]

_runner = CliRunner()


def invoke(*args: str) -> Result:
    return _runner.invoke(app, list(args))


def configure_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, slice_d: bool = False
) -> Path:
    """Point the CLI at a temporary data dir; return the data dir.

    The slice D stages (the vision attempt and enrichment) are off and the text extraction
    prompt is ``extraction/v1`` (integer cents) unless ``slice_d``: the slice A-C suites script
    exactly the model calls of the stages they are about, as ``extraction()``. With ``slice_d``
    the production defaults apply (extraction/v2 printed-value answers, two vision attempts,
    enrichment on; docs/plans/extraction-accuracy.md)."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PAPER_TRAIL_ENV", "test")
    monkeypatch.setenv("PAPER_TRAIL_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPER_TRAIL_STATIC_DIR", str(tmp_path / "no-static"))
    monkeypatch.setenv("PAPER_TRAIL_LOG_FORMAT", "json")
    if not slice_d:
        monkeypatch.setenv("PAPER_TRAIL_GRAPH__MAX_VISION_ATTEMPTS", "0")
        monkeypatch.setenv("PAPER_TRAIL_GRAPH__ENRICHMENT", "false")
        monkeypatch.setenv("PAPER_TRAIL_PROMPTS__EXTRACTION_VERSION", "v1")
    return data_dir


@contextmanager
def fake_ollama_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartOllama]:
    """``start(*replies, then=..., embeddings=...)`` serves a new fake Ollama and points the CLI
    at it; ``embeddings`` is the fake's vector table (``tests.fakes.embeddings``)."""
    with ExitStack() as stack:

        def start(*replies: Any, then: Any = None, embeddings: Any = None) -> FakeOllama:
            fake = stack.enter_context(
                serve_fake_ollama(FakeOllama(*replies, then=then, embeddings=embeddings))
            )
            monkeypatch.setenv("PAPER_TRAIL_OLLAMA__BASE_URL", fake.url)
            return fake

        yield start


def run_database(data_dir: Path, run: str | None) -> Path:
    """The database a run writes: ``runs/<run>/paper_trail.db``, or the main one."""
    return (
        data_dir / "paper_trail.db" if run is None else data_dir / "runs" / run / "paper_trail.db"
    )


def rows(database: Path, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(sql, params).fetchall()


def receipt_status(database: Path, receipt_id: int) -> str:
    ((status,),) = rows(database, "SELECT status FROM receipts WHERE id = ?", receipt_id)
    return str(status)


def trace_files(data_dir: Path, run: str = "main") -> list[Path]:
    return sorted((data_dir / "runs" / run / "traces").glob("*"))


def trace_document(path: Path) -> TraceDocument:
    return TraceDocument.model_validate_json(path.read_text(encoding="utf-8"))


def chat_contents(fake: FakeOllama, call: int) -> list[str]:
    return [str(message["content"]) for message in fake.chat_requests[call]["messages"]]


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path
