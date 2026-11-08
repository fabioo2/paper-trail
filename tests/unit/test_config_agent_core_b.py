"""Settings added by agent core slice B (PLAN 1.3: ``TOOLS__TRANSPORT``, ``MCP__URL``).

Pinned (docs/tests/agent-core-B.md): ``tools: ToolsSettings(transport: Literal["inprocess",
"mcp"] = "inprocess")`` and ``mcp: McpSettings(url: AnyHttpUrl = "http://mcp:8000",
timeout_s: float > 0)`` in ``paper_trail.config``, frozen like the other groups. Slice C (review
note 1, ``test_config_agent_core_c.py``): the timeout's default and lower bound now cover the
slowest tool, so it is no longer 30 s.
``McpSettings.url`` is the standalone server used by ``paper-trail tools --transport mcp``;
``paper-trail process --transport mcp`` serves its run's tools itself (see the CLI tests).
"""

import pytest
from pydantic import ValidationError

from paper_trail.config import McpSettings, Settings, ToolsSettings


def test_slice_b_defaults_match_the_documented_table() -> None:
    settings = Settings()

    assert settings.tools == ToolsSettings(transport="inprocess")
    assert str(settings.mcp.url).rstrip("/") == "http://mcp:8000"


def test_slice_b_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRAIL_TOOLS__TRANSPORT", "mcp")
    monkeypatch.setenv("PAPER_TRAIL_MCP__URL", "http://127.0.0.1:8421")
    monkeypatch.setenv("PAPER_TRAIL_MCP__TIMEOUT_S", "500")

    settings = Settings()

    assert settings.tools.transport == "mcp"
    assert str(settings.mcp.url).rstrip("/") == "http://127.0.0.1:8421"
    assert settings.mcp.timeout_s == 500.0


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PAPER_TRAIL_TOOLS__TRANSPORT", "carrier-pigeon"),
        ("PAPER_TRAIL_MCP__URL", "not a url"),
        ("PAPER_TRAIL_MCP__TIMEOUT_S", "0"),
    ],
)
def test_invalid_slice_b_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings()


def test_slice_b_groups_are_frozen() -> None:
    tools = ToolsSettings()
    mcp = McpSettings()

    with pytest.raises(ValidationError):
        tools.transport = "mcp"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        mcp.timeout_s = 1.0  # type: ignore[misc]
