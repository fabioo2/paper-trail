"""Real ``python -m paper_trail`` processes (S0-AC-15, S0-AC-23, S0-AC-24, S0-AC-25, AC-A-10).

One test per process boundary the containers rely on (``db upgrade``, ``serve``, ``worker``,
``process``; the standalone ``mcp`` server is in ``test_mcp_server.py``),
started the way the containers start them, so signal handling, logging setup and exit codes are
exercised for real. Checks that need no real process (``db current``, a second ``db upgrade``,
``worker --check-heartbeat``) run in-process through Typer's runner, on the same environment.
"""

import json
import os
import signal
import sqlite3
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.agent_cli import invoke, receipt_status, run_database, trace_files
from tests.extractions import COSTCO_ENRICHMENTS, COSTCO_OCR, printed_extraction
from tests.fakes.ollama_server import FakeOllama, serve_fake_ollama
from tests.support import SCHEMA_HEAD, cli_command, free_port, run_cli

REQUIRED_LOG_KEYS = {"timestamp", "level", "event", "logger"}
SHUTDOWN_GRACE_S = 2.0
HEARTBEAT_INTERVAL_S = 0.2


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def cli_env(data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The environment of the child processes; the in-process commands see the same values."""
    settings = {
        "PAPER_TRAIL_DATA_DIR": str(data_dir),
        "PAPER_TRAIL_STATIC_DIR": str(tmp_path / "no-static"),
        "PAPER_TRAIL_LOG_FORMAT": "json",
        "PAPER_TRAIL_LOG_LEVEL": "DEBUG",
        "PAPER_TRAIL_WORKER__POLL_INTERVAL_S": "0.1",
        "PAPER_TRAIL_WORKER__HEARTBEAT_INTERVAL_S": str(HEARTBEAT_INTERVAL_S),
        "PAPER_TRAIL_WORKER__SHUTDOWN_GRACE_S": str(SHUTDOWN_GRACE_S),
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    return {**{k: v for k, v in os.environ.items() if not k.startswith("PAPER_TRAIL_")}, **settings}


def _json_lines(stdout: str) -> list[dict[str, Any]]:
    records = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)  # every stdout line must be JSON (S0-AC-15)
        assert isinstance(record, dict)
        assert record.keys() >= REQUIRED_LOG_KEYS, record
        records.append(record)
    return records


def _wait_until(predicate: Any, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# --- database CLI ----------------------------------------------------------------------------


def test_s0_ac_23_and_15_db_upgrade_process_migrates_to_head_logging_json(
    cli_env: dict[str, str], data_dir: Path
) -> None:
    """S0-AC-23: `paper-trail db upgrade` on an empty data dir migrates to the head revision.
    S0-AC-15: alembic's own log lines are JSON too (env.py must not reconfigure logging)."""
    upgrade = run_cli(cli_command("db", "upgrade"), cli_env)

    assert upgrade.returncode == 0, upgrade.stderr
    assert (data_dir / "paper_trail.db").is_file()
    records = _json_lines(upgrade.stdout)
    assert any(r["logger"].startswith("alembic") for r in records), upgrade.stdout
    current = invoke("db", "current")
    assert current.exit_code == 0, current.output
    assert current.stdout.strip() == SCHEMA_HEAD


def _schema_snapshot(database: Path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(database)) as conn:
        schema = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
        versions = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    return [*schema, *versions]


def test_s0_ac_23_db_upgrade_twice_is_a_no_op(cli_env: dict[str, str], data_dir: Path) -> None:
    """S0-AC-23: running `db upgrade` again succeeds and changes nothing."""
    assert invoke("db", "upgrade").exit_code == 0
    database = data_dir / "paper_trail.db"
    before = _schema_snapshot(database)

    second = invoke("db", "upgrade")

    assert second.exit_code == 0, second.output
    assert _schema_snapshot(database) == before == [*before[:-1], (SCHEMA_HEAD,)]


# --- serve ------------------------------------------------------------------------------------


@pytest.fixture
def server(cli_env: dict[str, str]) -> Iterator[tuple[subprocess.Popen[str], int]]:
    port = free_port()
    env = {**cli_env, "PAPER_TRAIL_HOST": "127.0.0.1", "PAPER_TRAIL_HTTP_PORT": str(port)}
    process = subprocess.Popen(  # noqa: S603 - fixed argv built from sys.executable
        cli_command("serve"), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        yield process, port
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:  # noqa: S310 - local test server
            status: int = response.status
            return status
    except OSError:
        return 0


def test_s0_ac_15_serve_stdout_is_json_including_uvicorn(
    server: tuple[subprocess.Popen[str], int],
) -> None:
    """S0-AC-15: every stdout line of a running server (uvicorn included) is a JSON object."""
    process, port = server
    assert _wait_until(lambda: _get(f"http://127.0.0.1:{port}/api/health") == 200, 20)

    process.send_signal(signal.SIGTERM)
    stdout, _ = process.communicate(timeout=15)

    records = _json_lines(stdout)
    assert any(r["logger"].startswith("uvicorn") for r in records), stdout
    assert any(r["event"] == "http.request" for r in records), stdout


# --- worker -----------------------------------------------------------------------------------


def _check_heartbeat() -> int:
    return invoke("worker", "--check-heartbeat").exit_code


def test_s0_ac_24_check_heartbeat_fails_when_file_is_missing(cli_env: dict[str, str]) -> None:
    """S0-AC-24: `worker --check-heartbeat` exits 1 when no heartbeat exists."""
    assert _check_heartbeat() == 1


def test_s0_ac_24_and_25_worker_heartbeats_then_stops_promptly_and_gracefully_on_sigterm(
    cli_env: dict[str, str],
) -> None:
    """S0-AC-24/25: fresh heartbeat while running, also with an idle poll longer than the
    shutdown grace; SIGTERM during that poll -> stopping/stopped logs and exit 0 within the
    grace, no shutdown timeout; after three missed intervals the heartbeat is stale (exit 1)."""
    env = {**cli_env, "PAPER_TRAIL_WORKER__POLL_INTERVAL_S": str(SHUTDOWN_GRACE_S * 15)}
    process = subprocess.Popen(  # noqa: S603 - fixed argv built from sys.executable
        cli_command("worker"), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert _wait_until(lambda: _check_heartbeat() == 0, 20), "heartbeat never fresh"
        time.sleep(3 * HEARTBEAT_INTERVAL_S)  # inside the idle poll, longer than a heartbeat
        assert _check_heartbeat() == 0, "the idle poll must not let the heartbeat go stale"

        process.send_signal(signal.SIGTERM)
        started = time.monotonic()
        stdout, stderr = process.communicate(timeout=SHUTDOWN_GRACE_S + 5)
        elapsed = time.monotonic() - started
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 0, stderr
    assert elapsed < SHUTDOWN_GRACE_S
    events = [r["event"] for r in _json_lines(stdout)]
    assert "worker.shutdown_timeout" not in events
    assert events.index("worker.stopping") < events.index("worker.stopped")
    assert events[-1] == "worker.stopped"

    time.sleep(3 * HEARTBEAT_INTERVAL_S + 0.3)
    assert _check_heartbeat() == 1, "heartbeat older than 3 intervals must be stale"


# --- process ----------------------------------------------------------------------------------


def test_ac_a_10_process_runs_a_receipt_through_the_agents_in_a_real_process(
    cli_env: dict[str, str], data_dir: Path, tmp_path: Path
) -> None:
    """AC-A-10: `paper-trail process` as the `cli` container runs it: the real composition root,
    SQLite, Ollama adapter and trace writer, against a scripted fake Ollama on 127.0.0.1. With
    the production defaults, so the three new products are enriched too (slice D)."""
    receipt = tmp_path / "costco.txt"
    receipt.write_text(COSTCO_OCR, encoding="utf-8")

    with serve_fake_ollama(FakeOllama(printed_extraction(), *COSTCO_ENRICHMENTS)) as fake:
        env = {**cli_env, "PAPER_TRAIL_OLLAMA__BASE_URL": fake.url}
        result = run_cli(
            cli_command("process", "--no-color", "--run", "e2e", "--text-file", str(receipt)), env
        )

    assert result.returncode == 0, result.stderr
    assert "COMPLETED" in result.stdout
    assert fake.calls == 4  # one extraction, three enrichments
    assert receipt_status(run_database(data_dir, "e2e"), 1) == "completed"
    assert len(trace_files(data_dir, "e2e")) == 1
