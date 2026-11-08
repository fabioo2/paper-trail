"""Command-line option types shared by ``main`` and the agent commands.

Kept apart from the command modules so ``main`` can declare every command's options without
importing those modules (and LangGraph or the MCP SDK behind them) until a command runs.
"""

from enum import StrEnum

from paper_trail.config import Settings


class Transport(StrEnum):
    """How the agents call their tools (``TOOLS__TRANSPORT``)."""

    INPROCESS = "inprocess"
    MCP = "mcp"  # the run's own registry, served over MCP on a loopback port in this process


def with_transport(settings: Settings, transport: Transport | None) -> Settings:
    """Settings with ``--transport`` applied (every command that takes it comes here)."""
    if transport is None:
        return settings
    tools = settings.tools.model_copy(update={"transport": transport.value})
    return settings.model_copy(update={"tools": tools})
