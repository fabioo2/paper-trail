"""``paper-trail process --transport mcp`` (AC-B-05, E3; claude-extraction CE-TC-033 wiring)
and slice A review note 3.

Real composition root, real SQLite, real Anthropic adapter against a scripted fake Messages API
on 127.0.0.1. The same script is run in-process and over MCP and the two traces are compared.

Pinned (docs/tests/agent-core-B.md):
- ``process --transport mcp`` serves the run's own tool registry over MCP on a free loopback
  port inside the CLI process and calls it through ``McpToolGateway``; it never contacts
  ``MCP__URL``. The flag overrides ``settings.tools.transport``; the trace's
  ``config.transport`` and the terminal header say ``mcp``.
- Same script, same photo: the same outcome and the same events (kind, name, NODE phase) in
  the same order over both transports; only durations differ.
- Review note 3: the header's prompt tag is the tag of the reads (the LLM events' name).
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.agent_cli import (
    StartClaude,
    closed_loopback_url,
    configure_cli,
    fake_claude_env,
    invoke,
    photo,
    receipt_status,
    run_database,
    trace_files,
)
from tests.extractions import answer, inconsistent_answer
from tests.pans import card_numbers

type Step = tuple[str, str, Any]


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    configured = configure_cli(monkeypatch, tmp_path, main_migrated=True)
    # A standalone MCP server that is never there: `process` must not need one.
    monkeypatch.setenv("PAPER_TRAIL_MCP__URL", closed_loopback_url())
    return configured


@pytest.fixture
def claude(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartClaude]:
    with fake_claude_env(monkeypatch) as start:
        yield start


@pytest.fixture
def receipt(tmp_path: Path) -> Path:
    return photo(tmp_path)


def _process(*args: str) -> tuple[int, str]:
    result = invoke("process", "--no-color", *args)
    return result.exit_code, result.output


def _trace(data_dir: Path, run: str) -> dict[str, Any]:
    (path,) = trace_files(data_dir, run)
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def _steps(document: dict[str, Any]) -> list[Step]:
    return [
        (event["kind"], event["name"], event["data"].get("phase")) for event in document["events"]
    ]


SCRIPTS = {
    "retry-then-complete": ((inconsistent_answer(), answer()), "completed"),
    "parked-for-review": ((inconsistent_answer(), inconsistent_answer()), "needs_review"),
}


@pytest.mark.parametrize("script", list(SCRIPTS))
def test_ac_b_05_mcp_gives_the_same_outcome_and_events_as_in_process(
    data_dir: Path, claude: StartClaude, receipt: Path, script: str
) -> None:
    replies, status = SCRIPTS[script]
    claude(*replies)
    inproc_code, inproc_output = _process("--run", "inproc", str(receipt))
    claude(*replies)
    mcp_code, mcp_output = _process("--run", "mcp", "--transport", "mcp", str(receipt))

    assert inproc_code == mcp_code == 0, mcp_output
    inproc, over_mcp = _trace(data_dir, "inproc"), _trace(data_dir, "mcp")
    assert inproc["outcome"]["status"] == over_mcp["outcome"]["status"] == status
    assert _steps(over_mcp) == _steps(inproc)
    assert (inproc["config"]["transport"], over_mcp["config"]["transport"]) == ("inprocess", "mcp")
    assert over_mcp["totals"] == inproc["totals"]
    assert receipt_status(run_database(data_dir, "mcp"), 1) == status
    assert "· mcp ━━" in mcp_output
    assert "· inprocess ━━" in inproc_output


def test_ac_b_05_a_photo_runs_its_tools_over_mcp(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    """The supervisor's writes and the matching agent's reads (one ``product_lookup`` per
    uncoded line, PC-TC-62/89) go through the MCP hop; there is no OCR tool any more."""
    claude(answer())

    code, output = _process("--run", "img", "--transport", "mcp", str(receipt))

    assert code == 0, output
    document = _trace(data_dir, "img")
    tools = [event["name"] for event in document["events"] if event["kind"] == "tool"]
    assert "ocr_read" not in tools
    assert "save_receipt" in tools
    assert tools[-4:] == ["product_lookup"] * 3 + ["save_matches"]
    assert all(
        event["data"]["allowed"] is True for event in document["events"] if event["kind"] == "tool"
    )
    assert document["config"]["transport"] == "mcp"
    assert card_numbers(json.dumps(document)) == []


def test_the_transport_setting_is_the_default(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_TOOLS__TRANSPORT", "mcp")
    claude(answer())

    code, output = _process("--run", "set", str(receipt))

    assert code == 0, output
    assert _trace(data_dir, "set")["config"]["transport"] == "mcp"


def test_review_note_3_the_header_shows_the_reads_prompt_tag(
    data_dir: Path, claude: StartClaude, receipt: Path
) -> None:
    claude(answer())

    code, output = _process("--run", "tag", str(receipt))

    assert code == 0, output
    llm_events = [event for event in _trace(data_dir, "tag")["events"] if event["kind"] == "llm"]
    (tag,) = {event["name"] for event in llm_events}
    assert f"· {tag} ·" in output
