"""Settings of the extraction-accuracy change (EA-AC-11; docs/plans/extraction-accuracy.md 2.7).

Pinned: ``OCR__PSM`` 6; ``PROMPTS__EXTRACTION_VERSION`` v2; new ``PROMPTS__VISION_VERSION``
(v1, the prompt ``vision/v1``); new ``GRAPH__VISION_FIRST_BELOW`` (0..1; default 1.0 since
2026-11-08, every photo goes vision first; 0 = off), which ``graph_limits`` passes on as
``GraphLimits.vision_first_below``;
``GRAPH__MAX_VISION_ATTEMPTS`` 2. There is no setting for the date window or the tolerances.
"""

import pytest
from pydantic import ValidationError

from paper_trail.agents.nodes.supervisor import graph_limits
from paper_trail.agents.prompts import PromptLibrary
from paper_trail.config import GraphSettings, Settings


def test_ea_ac_11_defaults() -> None:
    settings = Settings()

    assert settings.ocr.psm == 6
    assert settings.prompts.extraction_version == "v2"
    assert settings.prompts.vision_version == "v1"
    assert settings.graph.vision_first_below == 1.0  # every photo vision first (2026-11-08)
    assert settings.graph.max_vision_attempts == 2


def test_ea_ac_11_the_default_prompts_exist() -> None:
    prompts = Settings().prompts
    library = PromptLibrary()

    assert library.load("extraction", prompts.extraction_version).tag == "extraction/v2"
    assert library.load("vision", prompts.vision_version).tag == "vision/v1"


def test_ea_ac_11_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__VISION_FIRST_BELOW", "0")
    monkeypatch.setenv("PAPER_TRAIL_PROMPTS__VISION_VERSION", "v9")

    settings = Settings()

    assert settings.graph.vision_first_below == 0.0
    assert settings.prompts.vision_version == "v9"


@pytest.mark.parametrize("value", ["-0.1", "1.5", "high"])
def test_ea_ac_11_the_vision_first_threshold_is_a_confidence(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__VISION_FIRST_BELOW", value)

    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("configured", [0.0, 0.5, 0.65, 1.0])
def test_ea_ac_11_the_graph_routes_with_the_configured_threshold(configured: float) -> None:
    settings = Settings.model_validate({"graph": {"vision_first_below": configured}})

    assert graph_limits(settings).vision_first_below == configured


def test_ea_ac_11_graph_settings_default_matches_settings() -> None:
    assert GraphSettings().vision_first_below == Settings().graph.vision_first_below


def test_the_model_output_is_bounded_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings().ollama.max_output_tokens == 4096
    monkeypatch.setenv("PAPER_TRAIL_OLLAMA__MAX_OUTPUT_TOKENS", "2048")

    assert Settings().ollama.max_output_tokens == 2048


@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_the_output_bound_is_a_positive_count(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PAPER_TRAIL_OLLAMA__MAX_OUTPUT_TOKENS", value)

    with pytest.raises(ValidationError):
        Settings()


def test_the_output_bound_must_leave_room_for_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRAIL_OLLAMA__NUM_CTX", "4096")
    monkeypatch.setenv("PAPER_TRAIL_OLLAMA__MAX_OUTPUT_TOKENS", "4096")

    with pytest.raises(ValidationError, match="MAX_OUTPUT_TOKENS"):
        Settings()
