"""``paper-trail graph --mermaid`` (AC-A-12): the compiled graph as Mermaid, for the report.

Pinned (docs/tests/agent-core-A.md): it needs neither a database nor a Claude key
(nothing is opened or contacted) and prints only the diagram on stdout.
"""

from pathlib import Path

import pytest

from tests.e2e.agent_cli import configure_cli, invoke

NODES = (
    "extract",
    "validate",
    "supervisor",
    "review",
    "persist",
    "match",
    "commit_matches",
    "enrich",
    "commit_enrichment",
)
GONE = ("read", "extract_text", "extract_vision")


def test_ac_a_12_graph_prints_mermaid_without_a_database_or_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_dir = configure_cli(monkeypatch, tmp_path)

    result = invoke("graph", "--mermaid")

    assert result.exit_code == 0, result.output
    assert "graph TD" in result.stdout
    for node in NODES:
        assert node in result.stdout
    for node in GONE:
        assert node not in result.stdout
    assert not (data_dir / "paper_trail.db").exists()
