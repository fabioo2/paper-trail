"""Shared helpers for the agent-core CLI suites (``process``, ``review``, ``graph``).

The CLI runs in-process through Typer's runner with ``PAPER_TRAIL_*`` set by the test, and with
``ANTHROPIC_BASE_URL`` pointing at a scripted fake Messages API on 127.0.0.1 (and a dummy
``ANTHROPIC_API_KEY``); the real composition root and Anthropic adapter run, only the model is
a script (claude-extraction CQ17).
"""

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import upgrade_to_head
from paper_trail.adapters.trace.json_file import TraceDocument
from paper_trail.config import DATABASE_FILENAME
from paper_trail.interfaces.cli.main import app
from tests.fakes.anthropic_server import FakeClaude, serve_fake_claude
from tests.images import PRINTED_RECEIPTS

type StartClaude = Callable[..., FakeClaude]

TEST_API_KEY = "test-key-not-a-secret"

_runner = CliRunner()


def invoke(*args: str) -> Result:
    return _runner.invoke(app, list(args))


def migrate_main(data_dir: Path) -> None:
    """Migrate ``data_dir``'s main database to head: a ``--run`` needs it (its spend ledger,
    review rulings R3 and C1) and never migrates it itself."""
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(data_dir / DATABASE_FILENAME)
    try:
        upgrade_to_head(engine)
    finally:
        engine.dispose()


def configure_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    enrichment: bool = False,
    main_migrated: bool = False,
) -> Path:
    """Point the CLI at a temporary data dir; return the data dir. Enrichment is off unless
    ``enrichment``, so the suites pin exactly the steps they are about; the main database
    exists, at head, only with ``main_migrated`` (what a ``--run`` needs)."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PAPER_TRAIL_ENV", "test")
    monkeypatch.setenv("PAPER_TRAIL_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PAPER_TRAIL_STATIC_DIR", str(tmp_path / "no-static"))
    monkeypatch.setenv("PAPER_TRAIL_LOG_FORMAT", "json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setenv("PAPER_TRAIL_LOOKUP__OPEN_FACTS", "false")  # no product database, ever
    if not enrichment:
        monkeypatch.setenv("PAPER_TRAIL_GRAPH__ENRICHMENT", "false")
    if main_migrated:
        migrate_main(data_dir)
    return data_dir


@contextmanager
def fake_claude_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartClaude]:
    """``start(*replies, then=...)`` serves a new fake Messages API and points the CLI at it
    (``ANTHROPIC_BASE_URL``, with a dummy ``ANTHROPIC_API_KEY``): a second ``start`` stands for
    the API as a second process sees it."""
    with ExitStack() as stack:

        def start(*replies: Any, then: Any = None) -> FakeClaude:
            fake = stack.enter_context(serve_fake_claude(FakeClaude(*replies, then=then)))
            monkeypatch.setenv("ANTHROPIC_BASE_URL", fake.url)
            monkeypatch.setenv("ANTHROPIC_API_KEY", TEST_API_KEY)
            return fake

        yield start


def closed_loopback_url() -> str:
    """A loopback URL on which nothing listens (connection refused)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = server.server_address[1]
    server.server_close()
    return f"http://127.0.0.1:{port}"


def photo(folder: Path, name: str = "costco.png", index: int = 0) -> Path:
    """A rendered receipt photo at ``folder/name`` (distinct bytes per ``index``)."""
    path = folder / name
    path.write_bytes(PRINTED_RECEIPTS[index].render())
    return path


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


def write_json(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path
