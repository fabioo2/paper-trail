"""Real ``python -m paper_trail`` processes (S0-AC-15, S0-AC-23, S0-AC-24, S0-AC-25, AC-A-10).

One test per process boundary the containers rely on (``db upgrade``, ``serve``, ``worker``,
``process``; the standalone ``mcp`` server is in ``test_mcp_server.py``),
started the way the containers start them, so signal handling, logging setup and exit codes are
exercised for real. Checks that need no real process (``db current``, a second ``db upgrade``,
``worker --check-heartbeat``) run in-process through Typer's runner, on the same environment.
"""

import json
import os
import re
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
from alembic import command

from paper_trail.adapters.sqlite.migrations import alembic_config
from paper_trail.bootstrap import build_container, dispose
from paper_trail.config import Settings
from tests.e2e.agent_cli import (
    TEST_API_KEY,
    invoke,
    migrate_main,
    photo,
    receipt_status,
    rows,
    run_database,
    trace_files,
)
from tests.extractions import answer
from tests.fakes.anthropic_server import FakeClaude, serve_fake_claude
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
        # A child process is outside the loopback guard: no product database is ever asked.
        "PAPER_TRAIL_LOOKUP__OPEN_FACTS": "false",
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


def _database_at_0003_with_negative_observations(database: Path) -> None:
    """Revision 0003 with eight observations, ids 3 and 7 of line total -1.00 (discount lines
    observed as products: what migration 0004's preflight refuses, A2)."""
    command.upgrade(alembic_config(database), "0003")
    now = "2026-11-06T15:00:00.000000+00:00"
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            "INSERT INTO receipts (status, stage, currency, created_at, updated_at)"
            " VALUES ('completed', 'done', 'CAD', ?, ?)",
            (now, now),
        )
        conn.execute("INSERT INTO products (canonical_name, created_at) VALUES ('Eggs', ?)", (now,))
        for position in range(8):
            cents = -100 if position in (2, 6) else 899
            kind = "discount" if cents < 0 else "item"
            conn.execute(
                "INSERT INTO line_items (receipt_id, position, kind, raw_text, description,"
                " quantity, line_total_cents) VALUES (1, ?, ?, 'X', 'X', '1', ?)",
                (position, kind, cents),
            )
            conn.execute(
                "INSERT INTO price_observations (product_id, receipt_id, line_item_id, store_key,"
                " observed_on, quantity, unit_price_cents, line_total_cents, currency)"
                " VALUES (1, 1, ?, 'costco', '2026-11-01', '1', ?, ?, 'CAD')",
                (position + 1, cents, cents),
            )
        conn.commit()


def test_tc_122_db_upgrade_refuses_negative_observations_naming_them(
    cli_env: dict[str, str], data_dir: Path
) -> None:
    """TC-122 (A2, Q21): the refused upgrade exits non-zero listing ids 3 and 7, and the
    database stays at 0003 with its schema and rows as they were."""
    data_dir.mkdir(parents=True)
    database = data_dir / "paper_trail.db"
    _database_at_0003_with_negative_observations(database)
    before = _schema_snapshot(database)

    upgrade = run_cli(cli_command("db", "upgrade"), cli_env)

    assert upgrade.returncode != 0
    output = upgrade.stdout + upgrade.stderr
    assert re.search(r"\b3\b\D{1,5}\b7\b", output), output
    current = invoke("db", "current")
    assert current.stdout.strip() == "0003"
    assert _schema_snapshot(database) == before


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


def test_the_worker_needs_the_migrated_database(cli_env: dict[str, str]) -> None:
    result = invoke("worker")

    assert result.exit_code == 1
    assert "db upgrade" in result.output


def test_s0_ac_24_and_25_worker_processes_uploads_heartbeats_then_stops_gracefully_on_sigterm(
    cli_env: dict[str, str], data_dir: Path, tmp_path: Path
) -> None:
    """The worker runs the receipt an upload queued through the real composition root, against
    a scripted fake Messages API on 127.0.0.1. S0-AC-24/25: fresh heartbeat while running,
    also with an idle poll longer than the shutdown grace; SIGTERM during that poll ->
    stopping/stopped logs and exit 0 within the grace, no shutdown timeout; after three missed
    intervals the heartbeat is stale (exit 1)."""
    migrate_main(data_dir)
    container = build_container(Settings())
    try:
        upload = container.upload_receipt(photo(tmp_path).read_bytes())
    finally:
        dispose(container)
    receipt_id = upload.receipt.id
    assert receipt_id is not None
    database = run_database(data_dir, None)

    with serve_fake_claude(FakeClaude(answer())) as fake:
        env = {
            **cli_env,
            "PAPER_TRAIL_WORKER__POLL_INTERVAL_S": str(SHUTDOWN_GRACE_S * 15),
            "PAPER_TRAIL_GRAPH__ENRICHMENT": "false",
            "ANTHROPIC_BASE_URL": fake.url,
            "ANTHROPIC_API_KEY": TEST_API_KEY,
        }
        process = subprocess.Popen(  # noqa: S603 - fixed argv built from sys.executable
            cli_command("worker"),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert _wait_until(lambda: receipt_status(database, receipt_id) == "completed", 30)
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
    assert fake.calls == 1
    assert rows(database, "SELECT status FROM jobs") == [("succeeded",)]
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
    SQLite, Anthropic adapter and trace writer, against a scripted fake Messages API on
    127.0.0.1 (``ANTHROPIC_BASE_URL``). With the production defaults: after the read Claude
    guesses each of the three new products (CE-TC-217), and the same receipt again is matched by
    alias, with nothing new to guess."""
    migrate_main(data_dir)  # a --run needs the main database (R3, C1)
    receipts = [photo(tmp_path, "costco.png", 0), photo(tmp_path, "costco-again.png", 1)]

    outputs: list[str] = []
    guess: dict[str, Any] = {
        "name": "Guessed",
        "brand": None,
        "size": None,
        "category": "pantry",
        "tags": [],
    }
    with serve_fake_claude(FakeClaude(answer(), guess, guess, guess, answer())) as fake:
        env = {**cli_env, "ANTHROPIC_BASE_URL": fake.url, "ANTHROPIC_API_KEY": TEST_API_KEY}
        for receipt in receipts:
            result = run_cli(
                cli_command("process", "--no-color", "--run", "e2e", str(receipt)), env
            )
            assert result.returncode == 0, result.stderr
            outputs.append(result.stdout)

    assert "COMPLETED" in outputs[0]
    assert fake.calls == 5  # two reads, three guesses
    assert "alias 3" in outputs[1]
    assert receipt_status(run_database(data_dir, "e2e"), 1) == "completed"
    assert len(trace_files(data_dir, "e2e")) == 2
