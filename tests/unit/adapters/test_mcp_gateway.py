"""``McpToolGateway`` when the MCP server is not there (PLAN 7, agent core slice B).

The behaviour shared with the in-process gateway is in ``tests/contract/test_tool_gateway.py``.

Pinned (docs/tests/agent-core-B.md):
- ``McpToolGateway(settings: McpSettings)`` calls ``<settings.url>/mcp/<role>/``.
- A server that refuses the connection, or does not answer within ``settings.timeout_s``, raises
  ``ToolUnavailableError`` (``paper_trail.ports.tools``) naming ``host:port``, from ``call`` and
  from ``describe``. It is not a ``ToolError``: the caller did nothing wrong.

Slice B review note 4 (docs/tests/agent-core-C.md):
- The ``host:port`` in those messages uses the scheme's default port when the URL has none:
  80 for http, 443 for https.
- A successful result without ``structuredContent`` (another server's, not this project's) is
  read from its text content when that is one JSON object; otherwise it is a
  ``ToolOutputError`` naming the tool, not a ``ToolUnavailableError``: the server answered.
"""

import socket
import time
from collections.abc import Callable, Iterator
from contextlib import closing

import pytest

from paper_trail.adapters.mcp.gateway import McpToolGateway
from paper_trail.config import McpSettings
from paper_trail.domain.enums import AgentRole
from paper_trail.ports.tools import ToolError, ToolOutputError, ToolUnavailableError
from tests.fakes.ollama_server import closed_loopback_url
from tests.mcp_server import serve_text_only_mcp

TIMEOUT_S = 0.5


@pytest.fixture
def silent_url() -> Iterator[str]:
    """A loopback port whose TCP handshakes complete (the kernel's backlog) but which never
    answers a request."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"


def _gateway(url: str) -> McpToolGateway:
    return McpToolGateway(McpSettings.model_validate({"url": url, "timeout_s": TIMEOUT_S}))


def _calls(gateway: McpToolGateway) -> list[Callable[[], object]]:
    return [
        lambda: gateway.call(AgentRole.EXTRACTION, "ocr_read", {"image_path": "a.jpg"}),
        lambda: gateway.describe(AgentRole.EXTRACTION),
    ]


@pytest.mark.parametrize("which", [0, 1], ids=["call", "describe"])
def test_a_refused_connection_is_unavailable_and_names_the_server(which: int) -> None:
    url = closed_loopback_url()
    gateway = _gateway(url)
    host_port = url.removeprefix("http://").rstrip("/")

    with pytest.raises(ToolUnavailableError, match=host_port) as raised:
        _calls(gateway)[which]()

    assert not isinstance(raised.value, ToolError)


@pytest.mark.parametrize("which", [0, 1], ids=["call", "describe"])
def test_a_server_that_never_answers_times_out(silent_url: str, which: int) -> None:
    gateway = _gateway(silent_url)
    started = time.monotonic()

    with pytest.raises(ToolUnavailableError, match=silent_url.removeprefix("http://")):
        _calls(gateway)[which]()

    assert time.monotonic() - started < TIMEOUT_S + 5


# --- slice B review note 4 ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "host_port"),
    [("https://127.0.0.1", "127.0.0.1:443"), ("http://127.0.0.1", "127.0.0.1:80")],
    ids=["https", "http"],
)
def test_review_note_4_the_default_port_follows_the_scheme(url: str, host_port: str) -> None:
    """Nothing listens on these loopback ports in the test container: the call is refused (or
    fails its handshake), and the message names the port the gateway really tried."""
    with pytest.raises(ToolUnavailableError, match=host_port):
        _gateway(url).describe(AgentRole.EXTRACTION)


def test_review_note_4_a_text_only_json_result_is_the_result() -> None:
    with serve_text_only_mcp('{"answer": 42}') as url:
        result = _gateway_with_time(url).call(AgentRole.EXTRACTION, "echo", {})

    assert result == {"answer": 42}


@pytest.mark.parametrize("text", ["not json", "[1, 2]"], ids=["not-json", "not-an-object"])
def test_review_note_4_any_other_text_only_result_is_an_output_error(text: str) -> None:
    with serve_text_only_mcp(text) as url, pytest.raises(ToolOutputError, match="echo"):
        _gateway_with_time(url).call(AgentRole.EXTRACTION, "echo", {})


def _gateway_with_time(url: str) -> McpToolGateway:
    """A gateway with time to answer (the module's gateways give up after half a second)."""
    return McpToolGateway(McpSettings.model_validate({"url": url, "timeout_s": 10.0}))
