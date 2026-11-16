"""What a trace records about a run's configuration (slice A review note 2, agent-core plan 4.3).

Pinned (docs/tests/agent-core-B.md): ``bootstrap.run_config(settings)`` records the values the
graph actually runs with; ``transport`` is ``settings.tools.transport``. Slice D adds
``enrichment`` (``GRAPH__ENRICHMENT``); product codes add
``catalog`` (``CATALOG__ENABLED``, PC-TC-84). Since Claude reads receipts (claude-extraction
plan TF) the models are the extract and retry models and the limit is ``GRAPH__MAX_ATTEMPTS``.
The key is never part of it (CE-TC-041).
"""

from typing import Literal

import pytest

from paper_trail.bootstrap.agents import run_config
from paper_trail.config import CatalogSettings, GraphSettings, Settings, ToolsSettings
from paper_trail.ports.trace import RunConfig


@pytest.mark.parametrize(("max_attempts", "feedback"), [(1, True), (2, False)])
def test_the_graph_settings_and_models_are_recorded(max_attempts: int, feedback: bool) -> None:
    settings = Settings.model_validate(
        {
            "env": "test",
            "graph": {"max_attempts": max_attempts, "feedback": feedback},
            "claude": {"extract_model": "claude-opus-5-5", "retry_model": "claude-haiku-5-5"},
        }
    )

    assert run_config(settings) == RunConfig(
        models={"extract": "claude-opus-5-5", "retry": "claude-haiku-5-5"},
        prompt_versions={"extraction": settings.prompts.extraction_version},
        feedback=feedback,
        max_attempts=max_attempts,
        enrichment=True,
        catalog=True,
        transport="inprocess",
    )


@pytest.mark.parametrize("transport", ["inprocess", "mcp"])
def test_the_transport_comes_from_the_settings(transport: Literal["inprocess", "mcp"]) -> None:
    settings = Settings(env="test", tools=ToolsSettings(transport=transport))

    assert run_config(settings).transport == transport


@pytest.mark.parametrize("enrichment", [True, False])
def test_slice_d_whether_products_are_enriched_is_recorded(enrichment: bool) -> None:
    settings = Settings(env="test", graph=GraphSettings(enrichment=enrichment))

    assert run_config(settings).enrichment is enrichment


@pytest.mark.parametrize("enabled", [True, False])
def test_pc_tc_84_whether_the_catalog_is_on_is_recorded(enabled: bool) -> None:
    settings = Settings(env="test", catalog=CatalogSettings(enabled=enabled))

    assert run_config(settings).catalog is enabled
