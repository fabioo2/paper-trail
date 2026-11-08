"""``paper-trail tools list|call`` (AC-B-04, E3; PLAN 7 CLI), in-process and over MCP.

The MCP side is the in-test server (the production app over a harness registry) that
``PAPER_TRAIL_MCP__URL`` points the CLI at; the in-process side is the real composition root.

Pinned (docs/tests/agent-core-B.md):
- ``tools list [--role R] [--transport inprocess|mcp]``: one line per (role, tool) in
  ``AgentRole`` order, then by tool name: whitespace-separated role, tool name, description. A
  role without tools prints one line ``<role> (none)``. Needs no database and no LLM host. The
  output is identical for both transports.
- ``tools call --role R TOOL 'JSON' [--transport T]`` prints the tool's result as JSON on stdout
  and exits 0. Exit codes: 5 denied (``denied: role=<role> tool=<tool>`` on stderr, and the
  ``tool.denied`` log line on stderr in-process); 2 usage errors (an unknown role, payload that
  is not a JSON object, an unknown tool, input the tool refuses, naming the field); 1 the main
  database is not migrated (in-process, with the ``db upgrade`` hint), a tool's domain error
  such as an unknown image, or the MCP server is unreachable (naming ``host:port``).
- ``--transport`` defaults to ``settings.tools.transport``; ``mcp`` uses ``settings.mcp.url``.
"""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from paper_trail.config import DATABASE_FILENAME
from paper_trail.domain.enums import AgentRole
from paper_trail.domain.models import JsonValue
from paper_trail.tools.inprocess import InProcessToolGateway
from tests.agent_harness import Harness, build_harness
from tests.e2e.agent_cli import configure_cli, invoke
from tests.extractions import inconsistent_extraction, without_confidence
from tests.fakes.llm import ScriptedLlm
from tests.fakes.ollama_server import closed_loopback_url
from tests.mcp_server import serve_mcp
from tests.support import CopyMigrated

EXIT_UNAVAILABLE = 1
EXIT_USAGE = 2
EXIT_DENIED = 5
ROLE_ORDER = [role.value for role in AgentRole]


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return configure_cli(monkeypatch, tmp_path)


@pytest.fixture
def harness() -> Harness:
    return build_harness(ScriptedLlm())


@pytest.fixture
def mcp_url(monkeypatch: pytest.MonkeyPatch, data_dir: Path, harness: Harness) -> Iterator[str]:
    """The in-test MCP server, configured as the CLI's ``MCP__URL``."""
    with serve_mcp(harness.registry) as url:
        monkeypatch.setenv("PAPER_TRAIL_MCP__URL", url)
        yield url


@pytest.fixture
def migrated(data_dir: Path, copy_migrated: CopyMigrated) -> Path:
    copy_migrated(data_dir / DATABASE_FILENAME)
    return data_dir


def _rows(stdout: str) -> list[tuple[str, str | None, str | None]]:
    """(role, tool, description) per line; tool and description are None for ``(none)``."""
    parsed: list[tuple[str, str | None, str | None]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        role, rest = line.split(maxsplit=1)
        if rest.strip() == "(none)":
            parsed.append((role, None, None))
        else:
            name, description = rest.split(maxsplit=1)
            parsed.append((role, name, description.strip()))
    return parsed


def _expected_rows(
    harness: Harness, roles: list[AgentRole]
) -> list[tuple[str, str | None, str | None]]:
    specs = {spec.name: spec for spec in harness.registry.specs}
    expected: list[tuple[str, str | None, str | None]] = []
    for role in roles:
        names = harness.registry.for_role(role).names
        if not names:
            expected.append((role.value, None, None))
        expected.extend((role.value, name, specs[name].description) for name in names)
    return expected


# --- tools list (AC-B-04) -------------------------------------------------------------------------


@pytest.mark.usefixtures("data_dir")
def test_tools_list_prints_every_role_and_its_tools(harness: Harness) -> None:
    result = invoke("tools", "list")

    assert result.exit_code == 0, result.output
    rows = _rows(result.stdout)
    assert rows == _expected_rows(harness, list(AgentRole))
    assert [role for role, _, _ in rows] == sorted(
        (role for role, _, _ in rows), key=ROLE_ORDER.index
    )


@pytest.mark.usefixtures("data_dir")
@pytest.mark.parametrize("role", list(AgentRole))
def test_ac_b_04_tools_list_for_one_role(harness: Harness, role: AgentRole) -> None:
    result = invoke("tools", "list", "--role", role.value)

    assert result.exit_code == 0, result.output
    assert _rows(result.stdout) == _expected_rows(harness, [role])


@pytest.mark.usefixtures("mcp_url")
@pytest.mark.parametrize("role", [None, *AgentRole])
def test_ac_b_04_tools_list_is_identical_over_mcp(role: AgentRole | None) -> None:
    selector = [] if role is None else ["--role", role.value]

    inprocess = invoke("tools", "list", *selector, "--transport", "inprocess")
    over_mcp = invoke("tools", "list", *selector, "--transport", "mcp")

    assert inprocess.exit_code == over_mcp.exit_code == 0, over_mcp.output
    assert over_mcp.stdout == inprocess.stdout


@pytest.mark.usefixtures("data_dir")
def test_the_transport_defaults_to_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """With TOOLS__TRANSPORT=mcp and no flag, the command goes to MCP__URL (unreachable here)."""
    url = closed_loopback_url()
    monkeypatch.setenv("PAPER_TRAIL_TOOLS__TRANSPORT", "mcp")
    monkeypatch.setenv("PAPER_TRAIL_MCP__URL", url)

    result = invoke("tools", "list")

    assert result.exit_code == EXIT_UNAVAILABLE
    assert url.removeprefix("http://") in result.stderr


@pytest.mark.usefixtures("data_dir")
def test_tools_list_with_an_unreachable_server_names_it(monkeypatch: pytest.MonkeyPatch) -> None:
    url = closed_loopback_url()
    monkeypatch.setenv("PAPER_TRAIL_MCP__URL", url)

    result = invoke("tools", "list", "--transport", "mcp")

    assert result.exit_code == EXIT_UNAVAILABLE
    assert url.removeprefix("http://") in result.stderr


@pytest.mark.usefixtures("data_dir")
def test_an_unknown_role_is_a_usage_error() -> None:
    assert invoke("tools", "list", "--role", "janitor").exit_code == EXIT_USAGE


# --- tools call (E3) ------------------------------------------------------------------------------


def _check_arithmetic_payload() -> dict[str, JsonValue]:
    return {"receipt": without_confidence(inconsistent_extraction())}


@pytest.mark.usefixtures("migrated")
def test_an_allowed_call_prints_the_result_as_json(harness: Harness) -> None:
    payload = _check_arithmetic_payload()
    expected = InProcessToolGateway(harness.registry).call(
        AgentRole.VALIDATION, "check_arithmetic", payload
    )

    result = invoke(
        "tools", "call", "--role", "validation", "check_arithmetic", json.dumps(payload)
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == dict(expected)


@pytest.mark.usefixtures("migrated")
def test_e3_a_denied_call_exits_5_and_logs_the_denial() -> None:
    result = invoke(
        "tools",
        "call",
        "--role",
        "matching",
        "save_receipt",
        json.dumps({"receipt_id": 1, "draft": {}}),
    )

    assert result.exit_code == EXIT_DENIED
    assert "denied: role=matching tool=save_receipt" in result.stderr
    logged = [json.loads(line) for line in result.stderr.splitlines() if line.startswith("{")]
    assert any(
        record.get("event") == "tool.denied"
        and record.get("role") == "matching"
        and record.get("tool") == "save_receipt"
        for record in logged
    ), result.stderr
    assert result.stdout == ""


@pytest.mark.usefixtures("migrated", "mcp_url")
def test_e3_the_same_calls_over_mcp(harness: Harness) -> None:
    payload = _check_arithmetic_payload()
    expected = InProcessToolGateway(harness.registry).call(
        AgentRole.VALIDATION, "check_arithmetic", payload
    )

    allowed = invoke(
        "tools",
        "call",
        "--transport",
        "mcp",
        "--role",
        "validation",
        "check_arithmetic",
        json.dumps(payload),
    )
    denied = invoke(
        "tools",
        "call",
        "--transport",
        "mcp",
        "--role",
        "matching",
        "save_receipt",
        json.dumps({"receipt_id": 1, "draft": {}}),
    )

    assert allowed.exit_code == 0, allowed.output
    assert json.loads(allowed.stdout) == dict(expected)
    assert denied.exit_code == EXIT_DENIED
    assert "denied: role=matching tool=save_receipt" in denied.stderr


@pytest.mark.usefixtures("migrated")
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--role", "validation", "check_arithmetic", "{not json"], "JSON"),
        (["--role", "validation", "check_arithmetic", "[1, 2]"], "JSON object"),
        (["--role", "supervisor", "drop_tables", "{}"], "drop_tables"),
        (["--role", "validation", "check_arithmetic", '{"receipt": "adds up"}'], "receipt"),
        (["--role", "janitor", "check_arithmetic", "{}"], "janitor"),
    ],
)
def test_usage_errors_exit_2(arguments: list[str], message: str) -> None:
    result = invoke("tools", "call", *arguments)

    assert result.exit_code == EXIT_USAGE, result.output
    assert message in result.output


@pytest.mark.usefixtures("migrated")
def test_a_domain_error_of_the_tool_exits_1() -> None:
    result = invoke(
        "tools", "call", "--role", "extraction", "ocr_read", '{"image_path": "no/such.jpg"}'
    )

    assert result.exit_code == EXIT_UNAVAILABLE
    assert "no/such.jpg" in result.stderr


@pytest.mark.usefixtures("data_dir")
def test_tools_call_needs_a_migrated_database() -> None:
    result = invoke("tools", "call", "--role", "validation", "check_arithmetic", json.dumps({}))

    assert result.exit_code == EXIT_UNAVAILABLE
    assert "db upgrade" in result.stderr
