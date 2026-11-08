"""Settings defaults and validation (S0-AC-13)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from paper_trail.config import Settings, WorkerSettings


def test_s0_ac_13_defaults_match_the_documented_table() -> None:
    """S0-AC-13: Settings() with no environment yields the PLAN section 1.3 defaults."""
    settings = Settings()

    assert settings.env == "prod"
    assert settings.data_dir == Path("/data")
    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
    assert settings.host == "0.0.0.0"  # noqa: S104 - bind address inside the container
    assert settings.http_port == 8000
    assert settings.static_dir == Path("/app/static")
    assert settings.build_sha is None
    assert settings.worker == WorkerSettings()
    assert settings.worker.poll_interval_s == 1.0
    assert settings.worker.heartbeat_interval_s == 10.0
    assert settings.worker.shutdown_grace_s == 8.0


def test_s0_ac_13_database_path_defaults_to_data_dir() -> None:
    """S0-AC-13: database_path defaults to <data_dir>/paper_trail.db."""
    assert Settings().database_path == Path("/data/paper_trail.db")


def test_s0_ac_13_database_path_follows_a_custom_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S0-AC-13: the default database path is derived from PAPER_TRAIL_DATA_DIR."""
    monkeypatch.setenv("PAPER_TRAIL_DATA_DIR", str(tmp_path))

    assert Settings().database_path == tmp_path / "paper_trail.db"


def test_s0_ac_13_explicit_database_path_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """S0-AC-13: an explicit PAPER_TRAIL_DATABASE_PATH is used verbatim."""
    monkeypatch.setenv("PAPER_TRAIL_DATABASE_PATH", str(tmp_path / "other.db"))

    assert Settings().database_path == tmp_path / "other.db"


def test_s0_ac_13_reads_prefixed_and_nested_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """S0-AC-13: env prefix PAPER_TRAIL_ and nested delimiter __ are honoured."""
    monkeypatch.setenv("PAPER_TRAIL_ENV", "dev")
    monkeypatch.setenv("PAPER_TRAIL_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PAPER_TRAIL_WORKER__POLL_INTERVAL_S", "2.5")
    monkeypatch.setenv("PAPER_TRAIL_BUILD_SHA", "abc1234")

    settings = Settings()

    assert settings.env == "dev"
    assert settings.log_level == "DEBUG"
    assert settings.worker.poll_interval_s == 2.5
    assert settings.build_sha == "abc1234"


@pytest.mark.parametrize(
    ("env", "expected"), [("dev", "console"), ("test", "json"), ("prod", "json")]
)
def test_s0_ac_13_log_format_defaults_from_env(
    monkeypatch: pytest.MonkeyPatch, env: str, expected: str
) -> None:
    """S0-AC-13: with LOG_FORMAT unset, ENV=dev selects console logs and the rest JSON."""
    monkeypatch.setenv("PAPER_TRAIL_ENV", env)

    assert Settings().log_format == expected


def test_s0_ac_13_explicit_log_format_wins_over_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """S0-AC-13: an explicit PAPER_TRAIL_LOG_FORMAT is used even in dev."""
    monkeypatch.setenv("PAPER_TRAIL_ENV", "dev")
    monkeypatch.setenv("PAPER_TRAIL_LOG_FORMAT", "json")

    assert Settings().log_format == "json"


def test_s0_ac_13_reads_dotenv_file_when_present() -> None:
    """S0-AC-13: a .env file in the working directory is read when it exists."""
    Path(".env").write_text("PAPER_TRAIL_LOG_LEVEL=WARNING\n", encoding="utf-8")

    assert Settings().log_level == "WARNING"


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PAPER_TRAIL_LOG_LEVEL", "LOUD"),
        ("PAPER_TRAIL_LOG_FORMAT", "xml"),
        ("PAPER_TRAIL_ENV", "staging"),
        ("PAPER_TRAIL_HTTP_PORT", "not-a-port"),
        ("PAPER_TRAIL_WORKER__POLL_INTERVAL_S", "0"),
        ("PAPER_TRAIL_WORKER__POLL_INTERVAL_S", "0.05"),
        ("PAPER_TRAIL_WORKER__POLL_INTERVAL_S", "61"),
    ],
)
def test_s0_ac_13_invalid_values_raise_validation_error(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """S0-AC-13: invalid values raise pydantic ValidationError."""
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("value", ["0.1", "60"])
def test_s0_ac_13_poll_interval_bounds_are_inclusive(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """S0-AC-13: the documented poll interval range 0.1..60 is accepted at both ends."""
    monkeypatch.setenv("PAPER_TRAIL_WORKER__POLL_INTERVAL_S", value)

    assert Settings().worker.poll_interval_s == float(value)
