"""The ``paper-trail`` Typer CLI (S0-AC-13, S0-AC-21, S0-AC-26)."""

import json
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from typer.testing import CliRunner

import paper_trail
from paper_trail.interfaces.cli.main import app as cli

runner = CliRunner()


def test_s0_ac_26_help_lists_every_command() -> None:
    """S0-AC-26: `paper-trail --help` lists serve, worker, db, openapi, version."""
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    for command in ("serve", "worker", "db", "openapi", "version"):
        assert command in result.output


def test_s0_ac_26_db_help_lists_upgrade_and_current() -> None:
    """S0-AC-26 / S0-AC-23: the db group exposes `upgrade` and `current`."""
    result = runner.invoke(cli, ["db", "--help"])

    assert result.exit_code == 0, result.output
    assert "upgrade" in result.output
    assert "current" in result.output


def test_s0_ac_26_version_prints_package_version_and_unknown_sha() -> None:
    """S0-AC-26: `paper-trail version` prints the installed package version."""
    result = runner.invoke(cli, ["version"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"paper-trail {version('paper-trail')} (")
    assert paper_trail.__version__ == version("paper-trail")


def test_s0_ac_26_version_includes_build_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """S0-AC-26: the build SHA (PAPER_TRAIL_BUILD_SHA, set by the image) is printed."""
    monkeypatch.setenv("PAPER_TRAIL_BUILD_SHA", "abc1234")

    result = runner.invoke(cli, ["version"])

    assert result.output.strip() == f"paper-trail {version('paper-trail')} (abc1234)"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PAPER_TRAIL_LOG_LEVEL", "LOUD"),
        ("PAPER_TRAIL_WORKER__POLL_INTERVAL_S", "0"),
    ],
)
def test_s0_ac_13_invalid_settings_exit_non_zero_with_one_line_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """S0-AC-13: `paper-trail serve` with an invalid value fails fast with a one-line message."""
    started: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append((a, k)))
    monkeypatch.setenv(variable, value)

    result = runner.invoke(cli, ["serve"])

    assert result.exit_code != 0
    assert started == [], "the server must not start with invalid settings"
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert len(lines) == 1, result.output
    assert variable in lines[0]
    assert "Traceback" not in result.output


def test_s0_ac_14_serve_passes_settings_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S0-AC-14: serve builds Settings once and hands uvicorn an app object, not a string."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setenv("PAPER_TRAIL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAPER_TRAIL_STATIC_DIR", str(tmp_path / "missing-static"))
    monkeypatch.setenv("PAPER_TRAIL_HOST", "127.0.0.1")
    monkeypatch.setenv("PAPER_TRAIL_HTTP_PORT", "8123")

    result = runner.invoke(cli, ["serve"])

    assert result.exit_code == 0, result.output
    ((args, kwargs),) = calls
    assert isinstance(args[0] if args else kwargs["app"], FastAPI)
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8123
    assert kwargs["proxy_headers"] is False
    assert kwargs["access_log"] is False


def _assert_keys_sorted(node: object) -> None:
    if isinstance(node, dict):
        keys = list(node)
        assert keys == sorted(keys), f"keys not sorted: {keys}"
        for value in node.values():
            _assert_keys_sorted(value)
    elif isinstance(node, list):
        for value in node:
            _assert_keys_sorted(value)


def test_s0_ac_21_openapi_command_prints_sorted_spec_without_a_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S0-AC-21: `paper-trail openapi` writes sorted JSON to stdout and needs no database."""
    data_dir = tmp_path / "no-data-here"
    monkeypatch.setenv("PAPER_TRAIL_DATA_DIR", str(data_dir))

    result = runner.invoke(cli, ["openapi"])

    assert result.exit_code == 0, result.output
    spec = json.loads(result.stdout)
    assert spec["info"]["title"] == "Paper Trail"
    assert "/api/health" in spec["paths"]
    _assert_keys_sorted(spec)
    assert not (data_dir / "paper_trail.db").exists()


def test_s0_ac_21_openapi_command_writes_to_output_path(tmp_path: Path) -> None:
    """S0-AC-21: `--output PATH` writes the same document to a file."""
    target = tmp_path / "openapi.json"

    to_file = runner.invoke(cli, ["openapi", "--output", str(target)])
    to_stdout = runner.invoke(cli, ["openapi"])

    assert to_file.exit_code == 0, to_file.output
    assert json.loads(target.read_text(encoding="utf-8")) == json.loads(to_stdout.stdout)
