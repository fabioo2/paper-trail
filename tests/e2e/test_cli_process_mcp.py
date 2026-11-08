"""``paper-trail process --transport mcp`` (AC-B-05, E3) and slice A review notes 2 and 3.

Real composition root, real SQLite, real Ollama adapter against a scripted fake Ollama on
127.0.0.1. The same script is run in-process and over MCP and the two traces are compared.

Pinned (docs/tests/agent-core-B.md):
- ``process --transport mcp`` serves the run's own tool registry over MCP on a free loopback
  port inside the CLI process (the production ``create_mcp_app``, streamable HTTP) and calls it
  through ``McpToolGateway``: a standalone server writes to its own database and would not see
  the run's receipts. It therefore never contacts ``MCP__URL``. The flag overrides
  ``settings.tools.transport``; the trace's ``config.transport`` and the terminal header say
  ``mcp``.
- Same scripted LLM, same input: the same outcome and the same events (kind, name, NODE phase)
  in the same order over both transports; only durations differ.
- Review note 2: the trace's ``config.max_vision_attempts`` is the effective 0 until slice D.
- Review note 3: the header's prompt tag is the loaded prompt's tag, the same as the LLM events'.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.agent_cli import (
    StartOllama,
    configure_cli,
    fake_ollama_env,
    invoke,
    receipt_status,
    run_database,
    trace_files,
)
from tests.extractions import COSTCO_OCR, extraction, inconsistent_extraction
from tests.fakes.ollama_server import closed_loopback_url
from tests.images import PRINTED_RECEIPTS
from tests.pans import card_numbers

type Step = tuple[str, str, Any]


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    configured = configure_cli(monkeypatch, tmp_path)
    # A standalone MCP server that is never there: `process` must not need one.
    monkeypatch.setenv("PAPER_TRAIL_MCP__URL", closed_loopback_url())
    return configured


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> Iterator[StartOllama]:
    with fake_ollama_env(monkeypatch) as start:
        yield start


@pytest.fixture
def receipt_text(tmp_path: Path) -> Path:
    path = tmp_path / "costco.txt"
    path.write_text(COSTCO_OCR, encoding="utf-8")
    return path


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
    "retry-then-complete": ((inconsistent_extraction(), extraction()), (), "completed"),
    "parked-for-review": (
        (inconsistent_extraction(),),
        ("--max-text-attempts", "1"),
        "needs_review",
    ),
}


@pytest.mark.parametrize("script", list(SCRIPTS))
def test_ac_b_05_mcp_gives_the_same_outcome_and_events_as_in_process(
    data_dir: Path, ollama: StartOllama, receipt_text: Path, script: str
) -> None:
    replies, flags, status = SCRIPTS[script]
    ollama(*replies)
    inproc_code, inproc_output = _process(
        "--run", "inproc", *flags, "--text-file", str(receipt_text)
    )
    ollama(*replies)
    mcp_code, mcp_output = _process(
        "--run", "mcp", "--transport", "mcp", *flags, "--text-file", str(receipt_text)
    )

    assert inproc_code == mcp_code == 0, mcp_output
    inproc, over_mcp = _trace(data_dir, "inproc"), _trace(data_dir, "mcp")
    assert inproc["outcome"]["status"] == over_mcp["outcome"]["status"] == status
    assert _steps(over_mcp) == _steps(inproc)
    assert (inproc["config"]["transport"], over_mcp["config"]["transport"]) == ("inprocess", "mcp")
    assert over_mcp["totals"] == inproc["totals"]
    assert receipt_status(run_database(data_dir, "mcp"), 1) == status
    assert "· mcp ━━" in mcp_output
    assert "· inprocess ━━" in inproc_output


def test_ac_b_05_an_image_is_read_over_mcp(
    data_dir: Path, ollama: StartOllama, tmp_path: Path
) -> None:
    """ocr_read (Tesseract), the matching agent's reads (slice C: a lookup and a search per line
    on a fresh database) and the supervisor's writes all go through the MCP hop."""
    photo = tmp_path / "costco.jpg"
    photo.write_bytes(PRINTED_RECEIPTS[0].render())
    ollama(extraction())

    code, output = _process("--run", "img", "--transport", "mcp", str(photo))

    assert code == 0, output
    document = _trace(data_dir, "img")
    tools = [event for event in document["events"] if event["kind"] == "tool"]
    matching = ["product_lookup", "product_similarity_search"] * 3 + ["save_matches"]
    assert [event["name"] for event in tools] == [
        "ocr_read",
        "check_arithmetic",
        "save_receipt",
        *matching,
    ]
    assert all(event["data"]["allowed"] is True for event in tools)
    assert document["config"]["transport"] == "mcp"
    assert card_numbers(json.dumps(document)) == []


def test_the_transport_setting_is_the_default(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_TOOLS__TRANSPORT", "mcp")
    ollama(extraction())

    code, output = _process("--run", "set", "--text-file", str(receipt_text))

    assert code == 0, output
    assert _trace(data_dir, "set")["config"]["transport"] == "mcp"


# --- slice A review notes 2 and 3 ---------------------------------------------------------------


def test_review_note_2_the_trace_records_the_effective_vision_limit(
    data_dir: Path, ollama: StartOllama, receipt_text: Path
) -> None:
    """GRAPH__MAX_VISION_ATTEMPTS defaults to 1, but slice A/B has no vision node: 0 is what
    ran, so 0 is what the trace says."""
    ollama(extraction())

    code, output = _process("--run", "vis", "--text-file", str(receipt_text))

    assert code == 0, output
    assert _trace(data_dir, "vis")["config"]["max_vision_attempts"] == 0


@pytest.mark.parametrize(
    ("flags", "tag"), [((), "extraction/v1"), (("--no-examples",), "extraction/v1+noex")]
)
def test_review_note_3_the_header_shows_the_loaded_prompts_tag(
    data_dir: Path, ollama: StartOllama, receipt_text: Path, flags: tuple[str, ...], tag: str
) -> None:
    ollama(extraction())

    code, output = _process("--run", "tag", *flags, "--text-file", str(receipt_text))

    assert code == 0, output
    assert f"· {tag} ·" in output
    llm_events = [event for event in _trace(data_dir, "tag")["events"] if event["kind"] == "llm"]
    assert {event["name"] for event in llm_events} == {tag}
