"""Settings of agent core slice D (PLAN 1.3, agent-core plan 5).

Pinned (docs/tests/agent-core-D.md):
- ``GraphSettings.enrichment: bool = True`` (``PAPER_TRAIL_GRAPH__ENRICHMENT``): new products
  are enriched after matching; false skips the stage (a faster run for an experiment arm that
  is not about products).
- ``GRAPH__MAX_VISION_ATTEMPTS`` (default 1; 2 since extraction accuracy) is now what the
  graph routes with:
  ``graph_limits(settings).max_vision_attempts`` equals it.
- ``PROMPTS__ENRICHMENT_VERSION`` (default ``v1``, there since slice A) names a prompt that
  exists.
"""

import pytest

from paper_trail.agents.nodes.supervisor import graph_limits
from paper_trail.agents.prompts import PromptLibrary
from paper_trail.config import GraphSettings, Settings


def test_enrichment_is_on_by_default() -> None:
    assert Settings().graph.enrichment is True
    assert GraphSettings().enrichment is True


def test_enrichment_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__ENRICHMENT", "false")

    assert Settings().graph.enrichment is False


@pytest.mark.parametrize("configured", [0, 1, 2])
def test_the_vision_limit_is_the_configured_one(configured: int) -> None:
    settings = Settings(graph=GraphSettings(max_vision_attempts=configured))

    assert graph_limits(settings).max_vision_attempts == configured


def test_the_default_vision_limit_is_two_attempts() -> None:
    """Extraction accuracy (EA-AC-11): vision first gets one retry with its own issues."""
    assert graph_limits(Settings()).max_vision_attempts == 2


def test_the_default_enrichment_prompt_exists() -> None:
    version = Settings().prompts.enrichment_version

    assert PromptLibrary().load("enrichment", version).tag == f"enrichment/{version}"
