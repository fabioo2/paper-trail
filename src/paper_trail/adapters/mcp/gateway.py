"""``McpToolGateway``: the agents' tools called over MCP (PLAN 7, ADR-0013, S5-AC-04).

Each role talks to its own endpoint, ``<MCP__URL>/mcp/<role>/``, over streamable HTTP, so the
server enforces the allowlist. The graph is synchronous: every call opens a session, makes one
request and closes, inside one ``timeout_s`` budget. Results are the tools' JSON, equal to the
in-process gateway's; errors are raised again with their class (``tools.wire``). Denials are
logged by the server only, so each is logged once. A successful result from another MCP server
may carry its JSON as text only (no ``structuredContent``): one JSON object there is the
result, anything else a ``ToolOutputError``.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from mcp import Client, MCPError
from mcp.types import CallToolResult, TextContent

from paper_trail.config import McpSettings
from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.ports.tools import ToolDescription, ToolOutputError, ToolUnavailableError
from paper_trail.tools.wire import error_from_payload

_DEFAULT_PORTS = {"http": 80, "https": 443}


class McpToolGateway:
    """Implements ``paper_trail.ports.tools.ToolGateway``."""

    def __init__(self, settings: McpSettings) -> None:
        self._base = str(settings.url).rstrip("/")
        self._timeout_s = settings.timeout_s
        parts = urlsplit(self._base)
        self._host_port = f"{parts.hostname}:{parts.port or _DEFAULT_PORTS[parts.scheme]}"

    def call(
        self, role: AgentRole, name: str, payload: Mapping[str, JsonValue]
    ) -> Mapping[str, JsonValue]:
        result = self._session(role, lambda client: client.call_tool(name, dict(payload)))
        if result.is_error:
            raise error_from_payload(_object(result.structured_content), role, name, _text(result))
        output = _object(result.structured_content)
        return _from_text(name, _text(result)) if output is None else output

    def describe(self, role: AgentRole) -> tuple[ToolDescription, ...]:
        listed = self._session(role, lambda client: client.list_tools())
        return tuple(
            sorted(
                (ToolDescription(tool.name, tool.description or "") for tool in listed.tools),
                key=lambda tool: tool.name,
            )
        )

    def _session[R](self, role: AgentRole, request: Callable[[Client], Awaitable[R]]) -> R:
        """One request in a session of its own, within the timeout."""

        async def run() -> R:
            async with asyncio.timeout(self._timeout_s), self._client(role) as client:
                return await request(client)

        try:
            return asyncio.run(run())
        except TimeoutError:
            msg = f"the MCP server at {self._host_port} did not answer in {self._timeout_s:g}s"
            raise ToolUnavailableError(msg) from None
        except (OSError, MCPError, ExceptionGroup) as error:
            msg = f"the MCP server at {self._host_port} is unreachable: {_reason(error)}"
            raise ToolUnavailableError(msg) from None

    def _client(self, role: AgentRole) -> Client:
        return Client(f"{self._base}/mcp/{role.value}/", read_timeout_seconds=self._timeout_s)


def _object(value: Any) -> dict[str, JsonValue] | None:  # noqa: ANN401 - the SDK types it Any
    return value if isinstance(value, dict) else None


def _from_text(tool: str, text: str) -> dict[str, JsonValue]:
    """A result given as text only: it must be one JSON object."""
    try:
        output = _object(json.loads(text))
    except ValueError:
        output = None
    if output is None:
        msg = f"{tool}: the MCP server's result is neither structured nor a JSON object"
        raise ToolOutputError(msg)
    return output


def _text(result: CallToolResult) -> str:
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))


def _reason(error: BaseException) -> str:
    """The innermost cause, for a one-line message."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
