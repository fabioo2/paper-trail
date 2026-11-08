"""``paper-trail tools list|call`` and ``paper-trail mcp``: the agents' tools from the command line
and as an MCP server (PLAN 7, agent-core plan 3.1, experiment E3).

``tools`` uses the gateway ``TOOLS__TRANSPORT`` (or ``--transport``) selects: the registry over
the main database, or the standalone MCP server at ``MCP__URL``. Either way the role's allowlist
is enforced by the registry, so ``tools call --role matching save_receipt ...`` is the E3 denial.

Exit codes of ``tools call``: 0 the result is printed as JSON; 1 the main database is not
migrated (in-process), the MCP server is unreachable, or the tool failed (e.g. an unknown
image); 2 a usage error (payload not a JSON object, unknown tool, input the tool refuses);
5 the role may not call the tool.
"""

import json
import signal

import typer
import uvicorn

from paper_trail.bootstrap import build_container, database_is_current, dispose
from paper_trail.bootstrap.agents import tool_registry, tools_gateway
from paper_trail.config import Settings
from paper_trail.domain.enums import AgentRole
from paper_trail.domain.errors import DomainError
from paper_trail.domain.models import JsonValue
from paper_trail.interfaces.cli.receipts import (
    EXIT_INVALID_INPUT,
    EXIT_UNAVAILABLE,
    NOT_MIGRATED,
    fail,
)
from paper_trail.interfaces.mcp.server import create_mcp_app
from paper_trail.ports.ocr import OcrError
from paper_trail.ports.tools import (
    ToolFailedError,
    ToolInputError,
    ToolNotPermittedError,
    ToolOutputError,
    ToolUnavailableError,
    UnknownToolError,
)

EXIT_DENIED = 5
NO_TOOLS = "(none)"


def list_tools(settings: Settings, role: AgentRole | None) -> None:
    """One line per (role, tool): role, tool, description; ``<role> (none)`` for a role
    without tools. Opens no database."""
    roles = list(AgentRole) if role is None else [role]
    try:
        with tools_gateway(settings) as gateway:
            listed = [(each, gateway.describe(each)) for each in roles]
    except ToolUnavailableError as error:
        fail(EXIT_UNAVAILABLE, str(error))
    width = max((len(tool.name) for _, tools in listed for tool in tools), default=0)
    for each, tools in listed:
        if not tools:
            typer.echo(f"{each.value:<11} {NO_TOOLS}")
        for tool in tools:
            typer.echo(f"{each.value:<11} {tool.name:<{width}}  {tool.description}")


def call_tool(settings: Settings, role: AgentRole, name: str, payload_json: str) -> None:
    """Call ``name`` as ``role`` with the JSON object ``payload_json``; print the result."""
    payload = _payload(payload_json)
    if settings.tools.transport == "inprocess":
        _require_current_database(settings)
    try:
        with tools_gateway(settings) as gateway:
            result = gateway.call(role, name, payload)
    except ToolNotPermittedError as error:
        fail(EXIT_DENIED, str(error))
    except (UnknownToolError, ToolInputError) as error:
        fail(EXIT_INVALID_INPUT, str(error))
    except (
        DomainError,
        OcrError,
        ToolOutputError,
        ToolFailedError,
        ToolUnavailableError,
    ) as error:
        fail(EXIT_UNAVAILABLE, str(error))
    typer.echo(json.dumps(result, indent=2, ensure_ascii=False))


def serve_mcp(settings: Settings) -> None:
    """Serve the tools over MCP on ``HOST:HTTP_PORT`` over the main database until SIGTERM,
    then exit 0."""
    container = build_container(settings)
    # Uvicorn shuts down gracefully on SIGTERM and then raises the signal again with the handler
    # that was there before; a no-op one makes that a normal return (exit 0, like the worker).
    previous = signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
    try:
        if not database_is_current(container):
            fail(EXIT_UNAVAILABLE, NOT_MIGRATED)
        uvicorn.run(
            create_mcp_app(tool_registry(container)),
            host=settings.host,
            port=settings.http_port,
            proxy_headers=False,
            access_log=False,
            log_config=None,
        )
    finally:
        signal.signal(signal.SIGTERM, previous)
        dispose(container)


def _payload(text: str) -> dict[str, JsonValue]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        fail(EXIT_INVALID_INPUT, f"the payload is not JSON: {error}")
    if not isinstance(value, dict):
        fail(EXIT_INVALID_INPUT, "the payload must be a JSON object")
    payload: dict[str, JsonValue] = value
    return payload


def _require_current_database(settings: Settings) -> None:
    container = build_container(settings)
    try:
        current = database_is_current(container)
    finally:
        dispose(container)
    if not current:
        fail(EXIT_UNAVAILABLE, NOT_MIGRATED)
