"""Settings of the reliability fix (reliability plan R1, R5, R6, answer Q5).

Pinned: ``OllamaSettings.timeout_retries`` (env ``PAPER_TRAIL_OLLAMA__TIMEOUT_RETRIES``), an int
in 0..3, default 1 (an empty value counts as unset, like every setting: ``env_ignore_empty``).
Defaults ``OLLAMA__REQUEST_TIMEOUT_S`` 300 and ``MCP__TIMEOUT_S`` 330 (the documented-defaults
test in ``test_config_agent_core.py`` owns the 300). ``Settings`` still refuses an MCP timeout
that is not greater than max(OCR worst case, request timeout); retries do not enter that bound,
because embeddings (the only Ollama calls behind MCP) are never retried.
"""

import pytest
from pydantic import ValidationError

from paper_trail.config import Settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 1), ("", 1), ("0", 0), ("1", 1), ("3", 3)],
    ids=["default", "empty is unset", "off", "one", "most"],
)
def test_rl_tc_01_02_timeout_retries_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: int
) -> None:
    if value is not None:
        monkeypatch.setenv("PAPER_TRAIL_OLLAMA__TIMEOUT_RETRIES", value)

    retries = Settings().ollama.timeout_retries

    assert (type(retries), retries) == (int, expected)


@pytest.mark.parametrize("value", ["-1", "4", "1.5", "abc"])
def test_rl_tc_02_timeout_retries_outside_0_to_3_is_refused(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_OLLAMA__TIMEOUT_RETRIES", value)

    with pytest.raises(ValidationError, match="timeout_retries"):
        Settings()


@pytest.mark.parametrize(
    ("environment", "mcp_timeout"),
    [
        ({}, 330.0),
        ({"PAPER_TRAIL_MCP__TIMEOUT_S": "301"}, 301.0),
        ({"PAPER_TRAIL_OLLAMA__TIMEOUT_RETRIES": "3"}, 330.0),
        ({"PAPER_TRAIL_OLLAMA__TIMEOUT_RETRIES": "0"}, 330.0),
    ],
    ids=["defaults", "just above the request timeout", "most retries", "no retries"],
)
def test_rl_tc_03_retries_do_not_enter_the_mcp_bound(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], mcp_timeout: float
) -> None:
    """With 3 retries a bound of request timeout x tries (1200 s) would refuse 330 s."""
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)

    settings = Settings()

    assert (settings.ollama.request_timeout_s, settings.mcp.timeout_s) == (300.0, mcp_timeout)


@pytest.mark.parametrize(
    ("variable", "value"),
    [("PAPER_TRAIL_MCP__TIMEOUT_S", "300"), ("PAPER_TRAIL_OLLAMA__REQUEST_TIMEOUT_S", "330")],
    ids=["mcp equal to the request timeout", "request timeout equal to the mcp timeout"],
)
def test_rl_tc_03_the_mcp_timeout_must_exceed_the_request_timeout(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match="MCP__TIMEOUT_S"):
        Settings()
