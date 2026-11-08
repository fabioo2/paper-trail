"""An in-test MCP server and an independent MCP client (S5-AC-03, S5-AC-04).

``serve_mcp(registry)`` serves the production ASGI app, ``create_mcp_app(registry)``, with
uvicorn on a free 127.0.0.1 port in a background thread and yields its base URL; every role is
then at ``<base URL>/mcp/<role>/``. The graph suites call it through ``McpToolGateway`` (see
``tests/agent_harness.py``); ``list_tools`` and ``call_tool`` use the MCP SDK's own ``Client``
instead, so the server is also checked against a client this project did not write.

``serve_text_only_mcp(text)`` serves a minimal MCP server, not this project's, whose every tool
call succeeds with ``text`` as its only content and no ``structuredContent``: what another MCP
server may send, for ``McpToolGateway``'s handling of such results (slice B review note 4).
"""

import asyncio
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from typing import Any

import uvicorn
from mcp import Client
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import ASGIApp

from paper_trail.domain.enums import AgentRole
from paper_trail.interfaces.mcp.server import create_mcp_app
from paper_trail.tools.registry import ToolRegistry

STARTUP_TIMEOUT_S = 10.0
SHUTDOWN_TIMEOUT_S = 10.0


def role_url(base_url: str, role: AgentRole) -> str:
    """Where one role's MCP server is mounted (PLAN 7: ``/mcp/<role>/``)."""
    return f"{base_url.rstrip('/')}/mcp/{role.value}/"


@contextmanager
def serve_mcp(registry: ToolRegistry) -> Iterator[str]:
    with _serve(create_mcp_app(registry)) as url:
        yield url


@contextmanager
def serve_text_only_mcp(text: str) -> Iterator[str]:
    """A foreign MCP server: every role lists one tool, ``echo``, and every call succeeds with
    ``text`` as its only content (no ``structuredContent``)."""

    async def list_tools(_: object, __: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(tools=[Tool(name="echo", input_schema={"type": "object"})])

    async def call_tool(_: object, __: CallToolRequestParams) -> CallToolResult:
        return CallToolResult(content=[TextContent(text=text)])

    managers = [
        StreamableHTTPSessionManager(
            app=Server("text-only", on_list_tools=list_tools, on_call_tool=call_tool),
            stateless=True,
            json_response=True,
        )
        for _ in AgentRole
    ]

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for manager in managers:
                await stack.enter_async_context(manager.run())
            yield

    routes = [
        Route(f"/mcp/{role.value}/", endpoint=StreamableHTTPASGIApp(manager))
        for role, manager in zip(AgentRole, managers, strict=True)
    ]
    with _serve(Starlette(routes=routes, lifespan=lifespan)) as url:
        yield url


@contextmanager
def _serve(app: ASGIApp) -> Iterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                msg = "the in-test MCP server did not start"
                raise RuntimeError(msg)
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(SHUTDOWN_TIMEOUT_S)
        listener.close()


def list_tools(base_url: str, role: AgentRole) -> list[Tool]:
    """The tools an MCP client sees at the role's endpoint."""

    async def run() -> list[Tool]:
        async with Client(role_url(base_url, role)) as client:
            return list((await client.list_tools()).tools)

    return asyncio.run(run())


def call_tool(
    base_url: str, role: AgentRole, name: str, arguments: Mapping[str, Any]
) -> CallToolResult:
    """Call ``name`` at the role's endpoint as an external MCP client would."""

    async def run() -> CallToolResult:
        async with Client(role_url(base_url, role)) as client:
            return await client.call_tool(name, dict(arguments))

    return asyncio.run(run())


def result_text(result: CallToolResult) -> str:
    """The text content of a tool result (an error's message)."""
    return "\n".join(block.text for block in result.content if isinstance(block, TextContent))
