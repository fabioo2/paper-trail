"""Settings of agent core slice C (PLAN 1.3) and the MCP timeout (slice B review note 1).

Pinned (docs/tests/agent-core-C.md):
- ``matching: MatchingSettings`` in ``paper_trail.config`` (``PAPER_TRAIL_MATCHING__*``), frozen:
  ``auto_link_max_distance: float = 0.15`` and ``review_max_distance: float = 0.30``, cosine
  distances in 0..2; ``review_max_distance >= auto_link_max_distance`` (a band below the
  auto-link threshold would be empty and confusing, so the opposite order is refused).
- Review note 1: an MCP tool call waits for the slowest tool. ``ocr_read`` waits on Tesseract
  (``OCR__TIMEOUT_S``) and ``product_similarity_search``/``save_matches`` on the embedding model
  (``OLLAMA__REQUEST_TIMEOUT_S``), so ``Settings`` refuses an ``MCP__TIMEOUT_S`` that is not
  greater than both, naming ``MCP__TIMEOUT_S`` in the error. The default settings are valid.
"""

import pytest
from pydantic import ValidationError

from paper_trail.config import MatchingSettings, Settings


def test_matching_defaults_match_plan_1_3() -> None:
    settings = Settings()

    assert settings.matching == MatchingSettings(
        auto_link_max_distance=0.15, review_max_distance=0.30
    )


def test_matching_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_TRAIL_MATCHING__AUTO_LINK_MAX_DISTANCE", "0.1")
    monkeypatch.setenv("PAPER_TRAIL_MATCHING__REVIEW_MAX_DISTANCE", "0.4")

    settings = Settings()

    assert settings.matching.auto_link_max_distance == 0.1
    assert settings.matching.review_max_distance == 0.4


@pytest.mark.parametrize(
    ("auto_link", "review"),
    [("0.3", "0.2"), ("-0.1", "0.3"), ("0.15", "2.5")],
    ids=["band-reversed", "negative", "beyond-opposite"],
)
def test_invalid_thresholds_are_rejected(
    monkeypatch: pytest.MonkeyPatch, auto_link: str, review: str
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_MATCHING__AUTO_LINK_MAX_DISTANCE", auto_link)
    monkeypatch.setenv("PAPER_TRAIL_MATCHING__REVIEW_MAX_DISTANCE", review)

    with pytest.raises(ValidationError):
        Settings()


def test_equal_thresholds_are_allowed() -> None:
    """No confirmation band: every candidate is linked or new (the cut-line configuration)."""
    matching = MatchingSettings(auto_link_max_distance=0.2, review_max_distance=0.2)

    assert matching.review_max_distance == matching.auto_link_max_distance


def test_matching_settings_are_frozen() -> None:
    matching = MatchingSettings()

    with pytest.raises(ValidationError):
        matching.review_max_distance = 0.5  # type: ignore[misc]


# --- review note 1: the MCP timeout covers the slowest tool ---------------------------------------


def test_review_note_1_the_default_mcp_timeout_covers_the_slowest_tool() -> None:
    settings = Settings()

    assert settings.mcp.timeout_s > settings.ocr.worst_case_s
    assert settings.mcp.timeout_s > settings.ollama.request_timeout_s


@pytest.mark.parametrize(
    ("variable", "value"),
    [("PAPER_TRAIL_OCR__TIMEOUT_S", "100000"), ("PAPER_TRAIL_OLLAMA__REQUEST_TIMEOUT_S", "100000")],
    ids=["ocr", "embeddings"],
)
def test_review_note_1_an_mcp_timeout_below_a_tool_timeout_is_refused(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ValidationError, match="MCP__TIMEOUT_S"):
        Settings()


def test_review_note_1_the_old_30_second_default_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """30 s (slice B) is below the slowest tool (the OCR worst case, the model request): a call
    over MCP could time out early."""
    monkeypatch.setenv("PAPER_TRAIL_MCP__TIMEOUT_S", "30")

    with pytest.raises(ValidationError, match="MCP__TIMEOUT_S"):
        Settings()


def test_review_note_1_lower_tool_timeouts_allow_a_lower_mcp_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAPER_TRAIL_OCR__TIMEOUT_S", "5")  # four rotation runs: 20 s
    monkeypatch.setenv("PAPER_TRAIL_OLLAMA__REQUEST_TIMEOUT_S", "20")
    monkeypatch.setenv("PAPER_TRAIL_MCP__TIMEOUT_S", "30")

    assert Settings().mcp.timeout_s == 30.0
