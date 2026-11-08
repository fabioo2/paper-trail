"""Tools the agents call, each allowed to some roles only (PLAN 7, ADR-0013).

Payloads and results are JSON values, so the in-process gateway and the MCP gateway are
interchangeable: the same calls, the same results, the same error classes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue


class ToolError(Exception):
    """Base class of tool-call failures that are the caller's fault."""


class ToolNotPermittedError(ToolError):
    """The role may not call the tool. Never retried: a denial fails the run."""

    def __init__(self, role: AgentRole, tool: str) -> None:
        super().__init__(f"denied: role={role.value} tool={tool}")
        self.role = role
        self.tool = tool


class UnknownToolError(ToolError):
    """No tool has this name."""


class ToolInputError(ToolError):
    """The payload does not match the tool's input; the message names the field."""


class ToolOutputError(Exception):
    """A tool returned something that is not its declared output: a bug in the tool, not the
    caller's fault. The message names the tool."""


class ToolUnavailableError(Exception):
    """The tool server refused the connection or did not answer in time; names ``host:port``."""


class ToolFailedError(Exception):
    """A tool failed with an error that has no class of its own on the wire (MCP); the message
    starts with the original class name."""


@dataclass(frozen=True, slots=True)
class ToolDescription:
    name: str
    description: str


class ToolGateway(Protocol):
    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        """Call tool ``name`` as ``role``. Raises UnknownToolError, ToolNotPermittedError or
        ToolInputError (in that order of checking); errors of the tool itself propagate."""
        ...

    def describe(self, role: AgentRole) -> tuple[ToolDescription, ...]:
        """The tools ``role`` may call, sorted by name."""
        ...
