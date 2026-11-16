"""Settings added by the agent core (PLAN 1.3, agent-core plan 5).

Pinned (docs/tests/agent-core-A.md): nested groups ``upload``, ``graph`` and ``prompts``
(``UploadSettings``, ``GraphSettings``, ``PromptSettings`` in ``paper_trail.config``), frozen
like ``WorkerSettings``. Since Claude reads receipts (claude-extraction plan) the OCR and Ollama
groups are gone (CE-TC-002) and Claude's own settings are ``test_config_claude.py``.
"""

import pytest
from pydantic import ValidationError

from paper_trail.config import GraphSettings, PromptSettings, Settings, UploadSettings


def test_agent_core_defaults_match_the_documented_table() -> None:
    settings = Settings()

    assert settings.upload == UploadSettings(max_pixels=40_000_000)
    assert settings.graph == GraphSettings(max_attempts=2, feedback=True, enrichment=True)
    assert settings.prompts == PromptSettings(extraction_version="v3")


def test_agent_core_settings_read_nested_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """PLAN 1.3: PAPER_TRAIL_<GROUP>__<FIELD> variables configure each group."""
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__MAX_ATTEMPTS", "1")
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__FEEDBACK", "false")
    monkeypatch.setenv("PAPER_TRAIL_PROMPTS__EXTRACTION_VERSION", "v9")

    settings = Settings()

    assert (
        settings.graph.max_attempts,
        settings.graph.feedback,
        settings.prompts.extraction_version,
    ) == (1, False, "v9")


def test_agent_core_settings_groups_are_frozen() -> None:
    with pytest.raises(ValidationError):
        Settings().graph.feedback = False  # type: ignore[misc]
