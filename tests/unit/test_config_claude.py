"""Claude and graph settings (claude-extraction plan E7, TF, DEL; CE-TC-001, 002, 040, 064).

Pinned (test-cases.md "Settings" contract table):
- ``claude: ClaudeSettings`` (``PAPER_TRAIL_CLAUDE__*``): ``extract_model`` ``claude-haiku-5-5``,
  ``retry_model`` ``claude-opus-5-5`` (non-empty), ``timeout_s`` 300 (> 0, <= 600; ruling R11),
  ``monthly_budget_usd`` 5.00 (>= 0); ``GRAPH__MAX_ATTEMPTS`` 2 (1 or 2).
- The key is ``ANTHROPIC_API_KEY`` only (no ``PAPER_TRAIL_`` prefix, read from ``.env`` too);
  without it ``claude.api_key`` is None and start-up still succeeds.
- ``MCP__TIMEOUT_S`` must stay above ``CLAUDE__TIMEOUT_S``; the refusal names both.
- The OCR, Ollama and matching groups and the routing knobs are gone; a stale variable is
  ignored (CQ7), like any unknown variable.
- An empty variable counts as unset (``env_ignore_empty``, every setting): an empty model name
  in the environment gives the default; given directly it is refused.
"""

import os
import socket
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from paper_trail.config import ClaudeSettings, Settings, env_var_name
from paper_trail.domain.rules.budget import MODEL_PRICES

KEY = "sk-ant-api03-test-key-not-real"


def test_ce_tc_001_defaults() -> None:
    settings = Settings()

    assert settings.claude == ClaudeSettings(
        api_key=None,
        extract_model="claude-haiku-5-5",
        retry_model="claude-opus-5-5",
        timeout_s=300.0,
        monthly_budget_usd=Decimal("5.00"),
    )
    assert settings.graph.max_attempts == 2


def test_ce_tc_001_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable, value in {
        "PAPER_TRAIL_CLAUDE__EXTRACT_MODEL": "claude-opus-5-5",
        "PAPER_TRAIL_CLAUDE__RETRY_MODEL": "claude-haiku-5-5",
        "PAPER_TRAIL_CLAUDE__TIMEOUT_S": "30",
        "PAPER_TRAIL_CLAUDE__MONTHLY_BUDGET_USD": "0",
        "PAPER_TRAIL_GRAPH__MAX_ATTEMPTS": "1",
    }.items():
        monkeypatch.setenv(variable, value)

    settings = Settings()

    assert (
        settings.claude.extract_model,
        settings.claude.retry_model,
        settings.claude.timeout_s,
        settings.claude.monthly_budget_usd,
        settings.graph.max_attempts,
    ) == ("claude-opus-5-5", "claude-haiku-5-5", 30.0, Decimal(0), 1)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PAPER_TRAIL_CLAUDE__TIMEOUT_S", "0"),
        ("PAPER_TRAIL_CLAUDE__TIMEOUT_S", "-1"),
        ("PAPER_TRAIL_CLAUDE__TIMEOUT_S", "601"),
        ("PAPER_TRAIL_CLAUDE__TIMEOUT_S", "abc"),
        ("PAPER_TRAIL_CLAUDE__MONTHLY_BUDGET_USD", "-0.01"),
        ("PAPER_TRAIL_CLAUDE__MONTHLY_BUDGET_USD", "abc"),
        ("PAPER_TRAIL_GRAPH__MAX_ATTEMPTS", "0"),
        ("PAPER_TRAIL_GRAPH__MAX_ATTEMPTS", "3"),
        ("PAPER_TRAIL_GRAPH__MAX_ATTEMPTS", "1.5"),
    ],
)
def test_ce_tc_001_refused_values_name_their_variable(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError) as raised:
        Settings()

    assert [env_var_name(error["loc"]) for error in raised.value.errors()] == [variable]


@pytest.mark.parametrize("field", ["extract_model", "retry_model"])
def test_ce_tc_001_an_empty_model_name_is_refused(field: str) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate({"claude": {field: ""}})

    assert [env_var_name(error["loc"]) for error in raised.value.errors()] == [
        f"PAPER_TRAIL_CLAUDE__{field.upper()}"
    ]


@pytest.mark.parametrize("variable", ["EXTRACT_MODEL", "RETRY_MODEL"])
def test_ce_tc_001_an_empty_model_variable_counts_as_unset(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    monkeypatch.setenv(f"PAPER_TRAIL_CLAUDE__{variable}", "")

    assert Settings().claude == ClaudeSettings()


@pytest.mark.parametrize(
    ("mcp_timeout", "claude_timeout"),
    [("30", "60"), ("60", "60"), ("300", None), ("330", "500")],
    ids=["below", "equal", "equal-to-the-default", "default mcp below a long claude timeout"],
)
def test_ce_tc_001_the_mcp_timeout_must_outlast_claude(
    monkeypatch: pytest.MonkeyPatch, mcp_timeout: str, claude_timeout: str | None
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_MCP__TIMEOUT_S", mcp_timeout)
    if claude_timeout is not None:
        monkeypatch.setenv("PAPER_TRAIL_CLAUDE__TIMEOUT_S", claude_timeout)

    with pytest.raises(ValidationError) as raised:
        Settings()

    message = str(raised.value)
    assert "PAPER_TRAIL_MCP__TIMEOUT_S" in message
    assert "PAPER_TRAIL_CLAUDE__TIMEOUT_S" in message


def test_ce_tc_001_an_mcp_timeout_just_above_claude_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_MCP__TIMEOUT_S", "301")

    assert (Settings().mcp.timeout_s, Settings().claude.timeout_s) == (301.0, 300.0)


def test_ce_tc_001_claude_settings_are_frozen() -> None:
    with pytest.raises(ValidationError):
        Settings().claude.extract_model = "other"  # type: ignore[misc]


# --- CE-TC-002: the local stack's settings are gone ---------------------------------------------


@pytest.mark.parametrize("group", ["ocr", "ollama", "matching"])
def test_ce_tc_002_removed_groups_are_gone(group: str) -> None:
    assert group not in Settings.model_fields
    assert not hasattr(Settings(), group)


@pytest.mark.parametrize(
    "field",
    [
        "max_text_attempts",
        "max_vision_attempts",
        "vision_first_below",
        "min_confidence",
    ],
)
def test_ce_tc_002_removed_graph_settings_are_gone(field: str) -> None:
    assert field not in type(Settings().graph).model_fields


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PAPER_TRAIL_OCR__PSM", "not-a-number"),
        ("PAPER_TRAIL_OLLAMA__BASE_URL", "not a url"),
        ("PAPER_TRAIL_MATCHING__AUTO_LINK_MAX_DISTANCE", "far"),
        ("PAPER_TRAIL_GRAPH__MAX_TEXT_ATTEMPTS", "-5"),
        ("PAPER_TRAIL_GRAPH__MAX_VISION_ATTEMPTS", "-5"),
        ("PAPER_TRAIL_GRAPH__VISION_FIRST_BELOW", "high"),
        ("PAPER_TRAIL_GRAPH__MIN_CONFIDENCE", "9"),
        ("PAPER_TRAIL_PROMPTS__VISION_VERSION", "v9"),
    ],
)
def test_ce_tc_002_a_stale_variable_is_ignored(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """CQ7: an old ``.env`` still starts (the value would be refused if it were still read)."""
    monkeypatch.setenv(variable, value)

    assert Settings() == Settings.model_validate({})


# --- CE-TC-040: the key is ANTHROPIC_API_KEY only ---------------------------------------------


def test_ce_tc_040_the_key_comes_from_anthropic_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)

    api_key = Settings().claude.api_key

    assert api_key is not None
    assert api_key.get_secret_value() == KEY


def test_ce_tc_040_the_key_is_read_from_the_env_file() -> None:
    """The autouse fixture runs every test in an empty working directory: its ``.env`` is the
    one ``Settings`` reads, as in the containers."""
    Path(".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n", encoding="utf-8")

    api_key = Settings().claude.api_key

    assert api_key is not None
    assert api_key.get_secret_value() == KEY


@pytest.mark.parametrize(
    "environment",
    [{}, {"ANTHROPIC_API_KEY": ""}, {"PAPER_TRAIL_CLAUDE__API_KEY": KEY}],
    ids=["unset", "empty", "prefixed name only"],
)
def test_ce_tc_040_without_the_key_settings_still_load_with_none(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]
) -> None:
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)

    assert Settings().claude.api_key is None


# --- CE-TC-064: the test session itself never reaches the API -----------------------------------


def test_ce_tc_064_the_session_clears_the_anthropic_variables() -> None:
    assert {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"} & set(os.environ) == set()


def test_ce_tc_064_a_connection_beyond_loopback_fails_the_test() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(AssertionError, match="loopback"),
    ):
        sock.connect(("192.0.2.1", 443))  # TEST-NET-1: never routed


@pytest.mark.parametrize("field", ["extract_model", "retry_model"])
def test_c6_an_unpriced_model_is_refused_at_start_up(field: str) -> None:
    with pytest.raises(ValidationError) as raised:
        Settings.model_validate({"claude": {field: "claude-unknown-9"}})

    assert f"PAPER_TRAIL_CLAUDE__{field.upper()}" in str(raised.value)


@pytest.mark.parametrize("model", sorted(MODEL_PRICES))
def test_c6_every_priced_model_is_accepted(model: str) -> None:
    settings = Settings.model_validate({"claude": {"extract_model": model, "retry_model": model}})

    assert (settings.claude.extract_model, settings.claude.retry_model) == (model, model)
