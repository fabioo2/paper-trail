"""``paper-trail graph --mermaid`` (AC-A-12): the compiled graph as Mermaid, for the report.

Pinned (docs/tests/agent-core-A.md): it needs neither a database nor a configured LLM host
(nothing is opened or contacted) and prints only the diagram on stdout.
"""

from pathlib import Path

import pytest

from tests.e2e.agent_cli import configure_cli, invoke

NODES = (
    "read",
    "extract_text",
    "extract_vision",  # slice D
    "validate",
    "supervisor",
    "review",
    "persist",
    "match",
    "commit_matches",
    "enrich",  # slice D
    "commit_enrichment",
)


def test_ac_a_12_graph_prints_mermaid_without_a_database_or_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = configure_cli(monkeypatch, tmp_path)

    result = invoke("graph", "--mermaid")

    assert result.exit_code == 0, result.output
    assert "graph TD" in result.stdout
    for node in NODES:
        assert node in result.stdout
    assert not (data_dir / "paper_trail.db").exists()
