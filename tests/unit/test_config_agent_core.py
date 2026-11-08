"""Settings added by the agent core (PLAN 1.3, agent-core plan 5).

Pinned (docs/tests/agent-core-A.md): nested groups ``ocr``, ``upload``, ``ollama``, ``graph`` and
``prompts`` (``OcrSettings``, ``UploadSettings``, ``OllamaSettings``, ``GraphSettings``,
``PromptSettings`` in ``paper_trail.config``), frozen like ``WorkerSettings``. ``ocr.timeout_s``
(default 60) bounds one Tesseract run.
"""

import pytest
from pydantic import ValidationError

from paper_trail.config import (
    GraphSettings,
    OcrSettings,
    PromptSettings,
    Settings,
    UploadSettings,
)


def test_agent_core_defaults_match_the_documented_table() -> None:
    """PLAN 1.3: defaults for OCR, the LLM host, the graph limits and the prompts."""
    settings = Settings()

    assert settings.ocr == OcrSettings(lang="eng", psm=6, timeout_s=30.0)  # psm 6 since EA
    assert settings.upload.max_pixels == 40_000_000
    ollama = settings.ollama
    assert ollama.base_url is None  # not configured: the GPU is "unavailable"
    assert (ollama.probe_timeout_s, ollama.request_timeout_s) == (2.0, 180.0)
    assert ollama.text_model == "qwen2.5:7b-instruct"
    assert ollama.vision_model == "qwen2.5vl:7b"
    assert ollama.embed_model == "nomic-embed-text"
    assert ollama.keep_alive == "5m"
    assert ollama.num_ctx == 8192  # Ollama's own default of 4096 is too small (measured)
    assert settings.graph == GraphSettings(  # two vision attempts since extraction accuracy
        max_text_attempts=2, max_vision_attempts=2, min_confidence=0.6, feedback=True
    )
    assert settings.prompts == PromptSettings(  # extraction/v2 since extraction accuracy
        extraction_version="v2", matching_version="v1", enrichment_version="v1", examples=True
    )
    assert isinstance(settings.upload, UploadSettings)


def test_agent_core_settings_read_nested_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """PLAN 1.3: PAPER_TRAIL_<GROUP>__<FIELD> variables configure each group."""
    monkeypatch.setenv("PAPER_TRAIL_OLLAMA__BASE_URL", "http://llm-host.test:11434")
    monkeypatch.setenv("PAPER_TRAIL_OLLAMA__NUM_CTX", "16384")
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__MAX_TEXT_ATTEMPTS", "1")
    monkeypatch.setenv("PAPER_TRAIL_GRAPH__FEEDBACK", "false")
    monkeypatch.setenv("PAPER_TRAIL_PROMPTS__EXAMPLES", "false")
    monkeypatch.setenv("PAPER_TRAIL_PROMPTS__EXTRACTION_VERSION", "v1")
    monkeypatch.setenv("PAPER_TRAIL_OCR__PSM", "4")

    settings = Settings()

    assert str(settings.ollama.base_url).startswith("http://llm-host.test:11434")
    assert settings.ollama.num_ctx == 16384
    assert settings.graph.max_text_attempts == 1
    assert settings.graph.feedback is False
    assert settings.prompts.examples is False
    assert settings.prompts.extraction_version == "v1"
    assert settings.ocr.psm == 4


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PAPER_TRAIL_GRAPH__MAX_TEXT_ATTEMPTS", "0"),
        ("PAPER_TRAIL_GRAPH__MAX_VISION_ATTEMPTS", "-1"),
        ("PAPER_TRAIL_GRAPH__MIN_CONFIDENCE", "1.5"),
        ("PAPER_TRAIL_OLLAMA__BASE_URL", "not a url"),
        ("PAPER_TRAIL_OLLAMA__NUM_CTX", "0"),
        ("PAPER_TRAIL_OCR__TIMEOUT_S", "0"),
    ],
)
def test_agent_core_settings_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """At least one text attempt, no negative vision attempts, confidence within 0..1."""
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError):
        Settings()


def test_agent_core_settings_groups_are_frozen() -> None:
    with pytest.raises(ValidationError):
        Settings().graph.feedback = False  # type: ignore[misc]
