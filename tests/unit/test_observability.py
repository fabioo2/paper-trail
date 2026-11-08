"""Structured JSON logging (S0-AC-15) and correlation context helpers (S0-AC-16)."""

import logging
from typing import Any

import pytest
import structlog

from paper_trail.config import Settings
from paper_trail.observability import configure_logging
from tests.support import JsonLogs, MakeSettings

REQUIRED_KEYS = {"timestamp", "level", "event", "logger"}


@pytest.fixture
def json_settings(make_settings: MakeSettings) -> Settings:
    return make_settings(log_format="json", log_level="DEBUG")


def _assert_required_keys(records: list[dict[str, Any]]) -> None:
    assert records, "expected at least one log line on stdout"
    for record in records:
        assert record.keys() >= REQUIRED_KEYS, record


def test_s0_ac_15_structlog_lines_are_single_json_objects(
    json_settings: Settings, json_logs: JsonLogs
) -> None:
    """S0-AC-15: application log lines are one JSON object with the required keys."""
    configure_logging(json_settings)

    structlog.get_logger("paper_trail.test").info("unit.event", answer=42)

    records = json_logs()
    _assert_required_keys(records)
    (record,) = [r for r in records if r["event"] == "unit.event"]
    assert record["level"] == "info"
    assert record["logger"] == "paper_trail.test"
    assert record["answer"] == 42


def test_s0_ac_15_timestamp_is_iso8601_utc(json_settings: Settings, json_logs: JsonLogs) -> None:
    """S0-AC-15: timestamp is ISO-8601 in UTC."""
    configure_logging(json_settings)

    structlog.get_logger("paper_trail.test").info("unit.timestamp")

    (record,) = [r for r in json_logs() if r["event"] == "unit.timestamp"]
    timestamp = record["timestamp"]
    assert isinstance(timestamp, str)
    assert timestamp.endswith(("Z", "+00:00"))
    assert "T" in timestamp


@pytest.mark.parametrize(
    "logger_name", ["uvicorn.error", "uvicorn.access", "alembic.runtime.migration", "httpx"]
)
def test_s0_ac_15_stdlib_loggers_are_routed_through_structlog(
    json_settings: Settings, json_logs: JsonLogs, logger_name: str
) -> None:
    """S0-AC-15: third-party stdlib loggers (uvicorn, alembic, httpx) also emit JSON lines."""
    configure_logging(json_settings)

    logging.getLogger(logger_name).warning("stdlib message %s", "formatted")

    records = json_logs()
    _assert_required_keys(records)
    (record,) = [r for r in records if r["event"] == "stdlib message formatted"]
    assert record["logger"] == logger_name
    assert record["level"] == "warning"


def test_s0_ac_15_exceptions_are_logged_inside_the_json_object(
    json_settings: Settings, json_logs: JsonLogs
) -> None:
    """S0-AC-15: a logged traceback stays inside the single JSON line."""
    configure_logging(json_settings)
    log = structlog.get_logger("paper_trail.test")

    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        log.exception("unit.failed")

    (record,) = [r for r in json_logs() if r["event"] == "unit.failed"]
    assert record["level"] == "error"
    assert "kaboom" in str(record["exception"])


def test_s0_ac_15_log_level_filters_lower_levels(
    make_settings: MakeSettings, json_logs: JsonLogs
) -> None:
    """S0-AC-15: PAPER_TRAIL_LOG_LEVEL is applied to the root logger."""
    configure_logging(make_settings(log_level="WARNING"))
    log = structlog.get_logger("paper_trail.test")

    log.info("unit.hidden")
    log.warning("unit.shown")
    logging.getLogger("uvicorn.error").info("stdlib hidden")

    events = [r["event"] for r in json_logs()]
    assert "unit.shown" in events
    assert "unit.hidden" not in events
    assert "stdlib hidden" not in events


def test_s0_ac_15_configure_logging_is_idempotent(
    json_settings: Settings, json_logs: JsonLogs
) -> None:
    """S0-AC-15: configuring twice must not duplicate handlers (one line per event)."""
    configure_logging(json_settings)
    configure_logging(json_settings)

    logging.getLogger("uvicorn.error").warning("once")

    assert [r["event"] for r in json_logs()].count("once") == 1


def test_s0_ac_15_console_format_is_human_readable(
    make_settings: MakeSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    """S0-AC-15: LOG_FORMAT=console switches the renderer (not JSON)."""
    configure_logging(make_settings(log_format="console"))

    structlog.get_logger("paper_trail.test").info("unit.console")

    out = capsys.readouterr().out
    assert "unit.console" in out
    assert not out.lstrip().startswith("{")


def test_s0_ac_16_contextvars_are_merged_into_every_line(
    json_settings: Settings, json_logs: JsonLogs
) -> None:
    """S0-AC-16: values bound with structlog.contextvars appear on structlog and stdlib lines."""
    configure_logging(json_settings)
    structlog.contextvars.bind_contextvars(request_id="0123456789abcdef0123456789abcdef")

    structlog.get_logger("paper_trail.test").info("unit.bound")
    logging.getLogger("uvicorn.error").warning("stdlib bound")

    records = [r for r in json_logs() if r["event"] in {"unit.bound", "stdlib bound"}]
    assert len(records) == 2
    assert {r["request_id"] for r in records} == {"0123456789abcdef0123456789abcdef"}
