"""The tool registry served over MCP: one server per agent role (PLAN 7, ADR-0013, S5-AC-03).

``create_mcp_app(registry)`` is an ASGI app with one MCP server (streamable HTTP, stateless,
JSON responses) mounted at ``/mcp/<role>/`` for every ``AgentRole``. ``tools/list`` there lists
the role's tools with their input schemas, and ``tools/call`` goes through the role's toolbox,
so a tool of another role is a denial (logged as ``tool.denied`` by the registry), not an
unknown tool. A failed call is an error result carrying ``tools.wire``'s structured error.

The server has no authentication and its supervisor tools write, so it is fenced by where it
listens (127.0.0.1 only, see compose.yaml) and by DNS-rebinding protection: only ``Host``
headers ``127.0.0.1``, ``localhost`` and the compose service name ``mcp`` are served.

``serve_on_loopback(registry)`` runs the app on a free 127.0.0.1 port in a background thread;
``paper-trail process --transport mcp`` calls its run's tools through it.
"""

import json
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from typing import Any

import anyio.to_thread
import uvicorn
from mcp.server.lowlevel.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
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

from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.tools.registry import RoleToolbox, ToolRegistry
from paper_trail.tools.wire import error_payload

ALLOWED_HOSTS = ("127.0.0.1", "localhost", "mcp")  # "mcp": the compose service name
STARTUP_TIMEOUT_S = 10.0
SHUTDOWN_TIMEOUT_S = 10.0


def role_path(role: AgentRole) -> str:
    return f"/mcp/{role.value}/"


def create_mcp_app(registry: ToolRegistry) -> Starlette:
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{host}:*" for host in ALLOWED_HOSTS],
        allowed_origins=[f"http://{host}:*" for host in ALLOWED_HOSTS],
    )
    managers = {
        role: StreamableHTTPSessionManager(
            app=_role_server(registry.for_role(role), role),
            stateless=True,
            json_response=True,
            security_settings=security,
        )
        for role in AgentRole
    }

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            for manager in managers.values():
                await stack.enter_async_context(manager.run())
            yield

    routes = [
        Route(role_path(role), endpoint=StreamableHTTPASGIApp(manager))
        for role, manager in managers.items()
    ]
    return Starlette(routes=routes, lifespan=lifespan)


def _role_server(toolbox: RoleToolbox, role: AgentRole) -> Server[Any]:
    async def list_tools(_: object, __: PaginatedRequestParams | None) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name=spec.name,
                    description=spec.description,
                    input_schema=spec.input_model.model_json_schema(),
                )
                for spec in toolbox.specs
            ]
        )

    async def call_tool(_: object, params: CallToolRequestParams) -> CallToolResult:
        payload: dict[str, JsonValue] = dict(params.arguments or {})
        try:
            # The tools are synchronous (SQLite, Tesseract): off the event loop.
            output = await anyio.to_thread.run_sync(toolbox.call, params.name, payload)
        except Exception as error:  # every failure goes back to the caller as an error result
            failure = error_payload(error)
            return CallToolResult(
                content=[TextContent(text=str(failure["message"]))],
                structured_content=failure,
                is_error=True,
            )
        result = output.model_dump(mode="json")
        return CallToolResult(
            content=[TextContent(text=json.dumps(result))], structured_content=result
        )

    return Server(f"paper-trail-{role.value}", on_list_tools=list_tools, on_call_tool=call_tool)


@contextmanager
def serve_on_loopback(registry: ToolRegistry) -> Iterator[str]:
    """Serve ``create_mcp_app(registry)`` on a free 127.0.0.1 port in a background thread for
    the duration of the block; yields the base URL."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_mcp_app(registry), log_config=None, access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                msg = "the MCP tool server did not start"
                raise RuntimeError(msg)
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(SHUTDOWN_TIMEOUT_S)
        listener.close()
