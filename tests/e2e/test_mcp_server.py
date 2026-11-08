"""The MCP server: one endpoint per role, the registry behind each (S5-AC-03, AC-B-03, E3).

The in-test server is the production ASGI app (``create_mcp_app``) on a loopback port, checked
with the MCP SDK's own client; the last test starts ``paper-trail mcp`` as a real process.

Pinned (docs/tests/agent-core-B.md):
- ``paper_trail.interfaces.mcp.server.create_mcp_app(registry)`` returns an ASGI app that mounts
  one MCP server (streamable HTTP) per ``AgentRole`` at ``/mcp/<role>/``. ``tools/list`` there
  returns exactly ``registry.for_role(role).names``, each with the spec's description and its
  input model's JSON schema as ``inputSchema``. ``tools/call`` goes through
  ``registry.for_role(role).call``, so a tool of another role is denied (``tool.denied`` is
  logged), not reported as unknown. A successful result carries the tool's output as
  ``structuredContent``; a failure is a result with ``isError`` whose text is the error message.
- The server has no authentication and is published on 127.0.0.1 only; it refuses requests whose
  ``Host`` header is not ``localhost``, ``127.0.0.1`` or the compose service name ``mcp`` (any
  port): DNS-rebinding protection.
- ``paper-trail mcp`` serves that app on ``settings.host:settings.http_port`` over the main
  database, logs like ``serve`` (JSON lines on stdout), exits 0 on SIGTERM, and exits 1 with the
  ``db upgrade`` hint when the database is not migrated. Tool payloads are never logged.
"""

import json
import os
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from structlog.testing import capture_logs

from paper_trail.config import DATABASE_FILENAME
from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import Harness, build_harness
from tests.e2e.agent_cli import invoke
from tests.extractions import inconsistent_extraction, without_confidence
from tests.fakes.llm import ScriptedLlm
from tests.mcp_server import call_tool, list_tools, result_text, role_url, serve_mcp
from tests.support import CopyMigrated, cli_command, free_port

MATCHING_TOOLS = frozenset({"product_lookup", "product_similarity_search"})  # PLAN 7
SUPERVISOR_SLICE_A_TOOLS = frozenset({"save_receipt", "open_review"})
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "paper-trail-tests", "version": "0"},
    },
}
STREAMABLE_HEADERS = {"Accept": "application/json, text/event-stream"}
PRIVATE_STORE = "PRIVATE GROCER 4471"


@pytest.fixture
def harness() -> Harness:
    return build_harness(ScriptedLlm())


@pytest.fixture
def server(harness: Harness) -> Iterator[str]:
    with serve_mcp(harness.registry) as url:
        yield url


@pytest.mark.parametrize("role", list(AgentRole))
def test_s5_ac_03_each_role_lists_exactly_its_tools(
    harness: Harness, server: str, role: AgentRole
) -> None:
    specs = {spec.name: spec for spec in harness.registry.specs}

    tools = list_tools(server, role)

    assert sorted(tool.name for tool in tools) == list(harness.registry.for_role(role).names)
    for tool in tools:
        assert tool.description == specs[tool.name].description
        assert tool.input_schema == specs[tool.name].input_model.model_json_schema()


def test_s5_ac_03_matching_sees_only_its_read_tools(harness: Harness, server: str) -> None:
    """PLAN 7: `product_lookup` and `product_similarity_search` (once slice C registers them),
    never a write tool."""
    registered = {spec.name for spec in harness.registry.specs}

    names = {tool.name for tool in list_tools(server, AgentRole.MATCHING)}

    assert names == MATCHING_TOOLS & registered
    assert not names & {"save_receipt", "open_review", "save_matches", "save_enrichment"}


def test_s5_ac_03_save_receipt_at_the_matching_endpoint_is_denied_and_logged(
    server: str,
) -> None:
    arguments: dict[str, Any] = {"receipt_id": 1, "draft": {"store_name": PRIVATE_STORE}}

    with capture_logs() as logs:
        result = call_tool(server, AgentRole.MATCHING, "save_receipt", arguments)

    assert result.is_error is True
    assert "denied: role=matching tool=save_receipt" in result_text(result)
    denied = [entry for entry in logs if entry["event"] == "tool.denied"]
    assert [(entry["role"], entry["tool"]) for entry in denied] == [("matching", "save_receipt")]
    assert PRIVATE_STORE not in repr(logs)


def test_an_allowed_call_returns_the_output_as_structured_content(
    harness: Harness, server: str
) -> None:
    payload: dict[str, JsonValue] = {"receipt": without_confidence(inconsistent_extraction())}
    expected = InProcessToolGateway(harness.registry).call(
        AgentRole.VALIDATION, "check_arithmetic", payload
    )

    result = call_tool(server, AgentRole.VALIDATION, "check_arithmetic", payload)

    assert result.is_error is False
    assert result.structured_content == dict(expected)


def test_invalid_input_is_an_error_result_naming_the_field(server: str) -> None:
    result = call_tool(server, AgentRole.EXTRACTION, "ocr_read", {"image_path": 42})

    assert result.is_error is True
    assert "image_path" in result_text(result)


def test_an_unknown_role_has_no_endpoint(server: str) -> None:
    response = httpx.post(
        f"{server}/mcp/nobody/", json=INITIALIZE, headers=STREAMABLE_HEADERS, timeout=10
    )

    assert response.status_code == httpx.codes.NOT_FOUND


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "mcp"])
def test_local_and_compose_host_headers_are_served(server: str, host: str) -> None:
    port = httpx.URL(server).port
    response = httpx.post(
        role_url(server, AgentRole.SUPERVISOR),
        json=INITIALIZE,
        headers={**STREAMABLE_HEADERS, "Host": f"{host}:{port}"},
        timeout=10,
    )

    assert response.status_code == httpx.codes.OK


@pytest.mark.parametrize("host", ["evil.example", "192.168.0.10", "evil.example:8421"])
def test_other_host_headers_are_refused(server: str, host: str) -> None:
    """DNS rebinding: a web page cannot reach the unauthenticated write tools via a browser."""
    response = httpx.post(
        role_url(server, AgentRole.SUPERVISOR),
        json=INITIALIZE,
        headers={**STREAMABLE_HEADERS, "Host": host},
        timeout=10,
    )

    assert httpx.codes.BAD_REQUEST <= response.status_code < httpx.codes.INTERNAL_SERVER_ERROR
    assert response.status_code != httpx.codes.NOT_FOUND


# --- `paper-trail mcp` as a real process ----------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PAPER_TRAIL_")}
    env.update(
        {
            "PAPER_TRAIL_DATA_DIR": str(tmp_path / "data"),
            "PAPER_TRAIL_STATIC_DIR": str(tmp_path / "no-static"),
            "PAPER_TRAIL_LOG_FORMAT": "json",
            "PAPER_TRAIL_HOST": "127.0.0.1",
        }
    )
    return env


def _accepts(port: int) -> bool:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


def test_s5_ac_03_mcp_refuses_an_unmigrated_database(
    cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for key, value in cli_env.items():
        monkeypatch.setenv(key, value)

    result = invoke("mcp")

    assert result.exit_code == 1
    assert "db upgrade" in result.stderr


def test_s5_ac_03_paper_trail_mcp_starts_standalone(
    cli_env: dict[str, str], copy_migrated: CopyMigrated
) -> None:
    copy_migrated(Path(cli_env["PAPER_TRAIL_DATA_DIR"]) / DATABASE_FILENAME)
    port = free_port()
    env = {**cli_env, "PAPER_TRAIL_HTTP_PORT": str(port)}
    process = subprocess.Popen(  # noqa: S603 - fixed argv built from sys.executable
        cli_command("mcp"), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 20
        while not _accepts(port):
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "paper-trail mcp did not start listening"
            time.sleep(0.05)
        base = f"http://127.0.0.1:{port}"

        supervisor = {tool.name for tool in list_tools(base, AgentRole.SUPERVISOR)}
        matching = {tool.name for tool in list_tools(base, AgentRole.MATCHING)}
        denied = call_tool(
            base,
            AgentRole.MATCHING,
            "save_receipt",
            {"receipt_id": 1, "draft": {"store_name": PRIVATE_STORE}},
        )

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=10)

    assert process.returncode == 0, stderr
    assert supervisor >= SUPERVISOR_SLICE_A_TOOLS
    assert not matching & {"save_receipt", "open_review"}
    assert denied.is_error is True
    assert "denied: role=matching tool=save_receipt" in result_text(denied)
    records = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert any(
        record["event"] == "tool.denied"
        and record["role"] == "matching"
        and record["tool"] == "save_receipt"
        for record in records
    ), stdout
    assert PRIVATE_STORE not in stdout + stderr
