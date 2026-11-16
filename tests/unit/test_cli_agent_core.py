"""The agent core's commands and flags are registered (agent-core plan 4.1).

Slice A ships ``process``, ``review list|show|resolve`` and ``graph``; slice B ``tools`` and
``mcp``; slice C the ``process`` flags ``--interrupt-after``, ``--resume``, ``--reprocess`` and
``items correct`` (docs/tests/agent-core-C.md). Since Claude reads receipts the OCR, vision and
prompt flags are gone (claude-extraction plan V7, DEL; their refusal is CE-TC-058 in
``tests/e2e``).

Slice B review note 3 (pinned in docs/tests/agent-core-C.md): one helper,
``paper_trail.interfaces.cli.options.with_transport(settings, transport) -> Settings``, applies
``--transport`` for every command (``process`` through ``with_flags``, and ``tools``).
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from paper_trail.config import Settings, ToolsSettings
from paper_trail.interfaces.cli.main import app as cli
from paper_trail.interfaces.cli.options import Transport, with_transport
from paper_trail.interfaces.cli.process import ProcessOptions, with_flags

runner = CliRunner()

PROCESS_FLAGS = (
    "--run",
    "--no-feedback",
    "--transport",
    "--verbose",
    "--no-color",
    "--interrupt-after",  # slice C
    "--resume",
    "--reprocess",
    "--no-catalog",  # product codes (barcode-lookup PC-TC-78)
)


def test_help_lists_the_agent_core_commands() -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    for command in ("process", "review", "graph"):
        assert command in result.output


@pytest.mark.parametrize("flag", PROCESS_FLAGS)
def test_process_accepts_the_planned_flags(flag: str) -> None:
    result = runner.invoke(cli, ["process", "--help"], terminal_width=200)

    assert result.exit_code == 0, result.output
    assert flag in result.output


def test_review_has_list_show_and_resolve() -> None:
    result = runner.invoke(cli, ["review", "--help"])

    assert result.exit_code == 0, result.output
    for command in ("list", "show", "resolve"):
        assert command in result.output


def test_catalog_has_its_seven_commands() -> None:
    """Product codes PR 1b (barcode-lookup R15-R18, CLI contract table)."""
    result = runner.invoke(cli, ["catalog", "--help"], terminal_width=200)

    assert result.exit_code == 0, result.output
    for command in ("todo", "add", "list", "remove", "lookup", "plu-import", "backfill"):
        assert command in result.output


def test_resolve_has_file_accept_force_and_reason() -> None:
    result = runner.invoke(cli, ["review", "resolve", "--help"], terminal_width=200)

    assert result.exit_code == 0, result.output
    for flag in ("--file", "--accept", "--force", "--reason", "--run"):
        assert flag in result.output


def test_an_unknown_transport_is_a_usage_error() -> None:
    """--transport is inprocess or mcp (PLAN 1.3 TOOLS__TRANSPORT); mcp itself is slice B."""
    result = runner.invoke(cli, ["process", "--transport", "carrier-pigeon", "x.jpg"])

    assert result.exit_code == 2


# --- agent core slice B: tools, mcp ------------------------------------------------------------


def test_help_lists_the_slice_b_commands() -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.output
    for command in ("tools", "mcp"):
        assert command in result.output


@pytest.mark.parametrize("command", ["list", "call"])
def test_tools_commands_take_a_role_and_a_transport(command: str) -> None:
    result = runner.invoke(cli, ["tools", command, "--help"], terminal_width=200)

    assert result.exit_code == 0, result.output
    for flag in ("--role", "--transport"):
        assert flag in result.output


def test_process_offers_the_mcp_transport() -> None:
    result = runner.invoke(cli, ["process", "--help"], terminal_width=200)

    assert result.exit_code == 0, result.output
    assert "inprocess" in result.output
    assert "mcp" in result.output


# --- agent core slice C: items, and review note 3 ----------------------------------------------


def test_help_lists_the_items_command() -> None:
    result = runner.invoke(cli, ["items", "--help"])

    assert result.exit_code == 0, result.output
    assert "correct" in result.output


def test_items_correct_takes_its_planned_options() -> None:
    result = runner.invoke(cli, ["items", "correct", "--help"], terminal_width=200)

    assert result.exit_code == 0, result.output
    for flag in ("--product-id", "--new-product", "--category", "--run"):
        assert flag in result.output


def test_review_note_3_with_transport_overrides_only_when_given() -> None:
    settings = Settings(env="test", tools=ToolsSettings(transport="inprocess"))

    assert with_transport(settings, None) is settings
    assert with_transport(settings, Transport.MCP).tools.transport == "mcp"
    assert with_transport(settings, Transport.MCP).graph == settings.graph


def test_review_note_3_process_flags_apply_the_transport_the_same_way() -> None:
    settings = Settings(env="test")

    flagged = with_flags(settings, ProcessOptions(transport=Transport.MCP))

    assert flagged.tools == with_transport(settings, Transport.MCP).tools


# --- slice C review note 5 (pinned in docs/tests/agent-core-D.md) --------------------------------


@pytest.mark.parametrize(
    "flags",
    [
        ("--no-feedback",),
        ("--no-catalog",),
    ],
    ids=["no-feedback", "no-catalog"],
)
def test_review_note_5_resume_refuses_flags_that_would_change_the_run(
    flags: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A resumed thread continues with the configuration it started with: a flag that would
    change the graph's feedback or matching mid-run is a usage error, before anything
    is opened."""
    monkeypatch.setenv("PAPER_TRAIL_DATA_DIR", str(tmp_path / "data"))

    result = runner.invoke(cli, ["process", "--resume", "1", *flags], terminal_width=200)

    assert result.exit_code == 2, result.output
    assert "No such option" not in result.output  # refused by the command, not unknown
    assert flags[0] in result.output
    assert not (tmp_path / "data").exists()


def test_no_catalog_turns_the_catalog_off_and_nothing_else() -> None:
    """PC-TC-78: ``--no-catalog`` sets ``CATALOG__ENABLED`` false; without it nothing changes."""
    settings = Settings(env="test")

    flagged = with_flags(settings, ProcessOptions(catalog=False))

    assert flagged.catalog.enabled is False
    assert flagged.model_copy(update={"catalog": settings.catalog}) == with_flags(
        settings, ProcessOptions()
    )
    assert with_flags(settings, ProcessOptions()).catalog == settings.catalog
