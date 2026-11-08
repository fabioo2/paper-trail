"""What a trace records about a run's configuration (slice A review note 2, agent-core plan 4.3).

Pinned (docs/tests/agent-core-B.md): ``bootstrap.run_config(settings)`` records the values the
graph actually runs with. ``max_vision_attempts`` is the effective limit,
``graph_limits(settings).max_vision_attempts``, which since slice D is
``GRAPH__MAX_VISION_ATTEMPTS`` (docs/tests/agent-core-D.md); ``transport`` is
``settings.tools.transport``. Slice D adds ``enrichment`` (``GRAPH__ENRICHMENT``) and the
enrichment prompt's version.
"""

from typing import Literal

import pytest

from paper_trail.agents.nodes.supervisor import graph_limits
from paper_trail.bootstrap.agents import run_config
from paper_trail.config import GraphSettings, Settings, ToolsSettings


@pytest.mark.parametrize("configured", [0, 1, 3])
def test_review_note_2_the_effective_vision_limit_is_recorded(configured: int) -> None:
    settings = Settings(env="test", graph=GraphSettings(max_vision_attempts=configured))

    config = run_config(settings)

    assert config.max_vision_attempts == graph_limits(settings).max_vision_attempts
    assert config.max_vision_attempts == configured  # slice D: the vision node exists


def test_the_other_graph_settings_are_recorded_as_configured() -> None:
    settings = Settings(env="test", graph=GraphSettings(max_text_attempts=3, feedback=False))

    config = run_config(settings)

    assert config.max_text_attempts == graph_limits(settings).max_text_attempts == 3
    assert config.feedback is False


@pytest.mark.parametrize("transport", ["inprocess", "mcp"])
def test_the_transport_comes_from_the_settings(transport: Literal["inprocess", "mcp"]) -> None:
    settings = Settings(env="test", tools=ToolsSettings(transport=transport))

    assert run_config(settings).transport == transport


def test_slice_c_the_matching_prompt_and_the_embedding_model_are_recorded() -> None:
    """Slice C (docs/tests/agent-core-C.md): a run's trace names every prompt and model it may
    use, so experiment arms stay comparable."""
    settings = Settings(env="test")

    config = run_config(settings)

    assert config.prompt_versions == {
        "extraction": settings.prompts.extraction_version,
        "matching": settings.prompts.matching_version,
        "enrichment": settings.prompts.enrichment_version,  # slice D
    }
    assert config.models["embed"] == settings.ollama.embed_model
    assert config.models["text"] == settings.ollama.text_model


@pytest.mark.parametrize("enrichment", [True, False])
def test_slice_d_whether_products_are_enriched_is_recorded(enrichment: bool) -> None:
    settings = Settings(env="test", graph=GraphSettings(enrichment=enrichment))

    assert run_config(settings).enrichment is enrichment
