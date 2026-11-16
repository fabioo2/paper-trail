"""Settings of agent core slice D (PLAN 1.3, agent-core plan 5).

Pinned (docs/tests/agent-core-D.md):
- ``GraphSettings.enrichment: bool = True`` (``PAPER_TRAIL_GRAPH__ENRICHMENT``): new products
  are enriched after matching; false skips the stage (a faster run for an experiment arm that
  is not about products).
"""

import pytest

from paper_trail.config import GraphSettings, Settings


def test_enrichment_is_on_by_default() -> None:
    assert Settings().graph.enrichment is True
    assert GraphSettings().enrichment is True


def test_enrichment_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__ENRICHMENT", "false")

    assert Settings().graph.enrichment is False
