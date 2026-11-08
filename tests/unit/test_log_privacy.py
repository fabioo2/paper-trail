"""No INFO+ log line carries OCR text, prompts or model output (AC-A-14, S2-AC-12, ADR-0011).

A sentinel is planted in the OCR text and in the model's output; the run goes through retries,
review and resolution with DEBUG logging on; no line at INFO or above may contain a sentinel,
any part of the rendered prompt, or the receipt-specific feedback.
"""

import json
from typing import Any

import pytest

from paper_trail.observability import configure_logging
from tests.agent_harness import agent_settings, build_harness
from tests.extractions import (
    COSTCO_ENRICHMENTS,
    COSTCO_OCR,
    LINE3_ISSUE,
    extraction,
    inconsistent_extraction,
    printed_extraction,
)
from tests.factories import receipt_draft
from tests.fakes.llm import ScriptedLlm
from tests.support import REPO_ROOT, MakeSettings, require_id

OCR_SENTINEL = "SENTINEL-OCR-7F3A"
OUTPUT_SENTINEL = "SENTINEL-LLM-91C2"
PROMPT_DIR = REPO_ROOT / "src" / "paper_trail" / "agents" / "prompts" / "extraction" / "v1"
QUIET_LEVELS = {"debug", "notset"}


def _lines(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_ac_a_14_no_receipt_content_at_info_or_above(
    make_settings: MakeSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(make_settings(log_level="DEBUG", log_format="json"))
    first = inconsistent_extraction()
    first["items"][0]["description"] = f"Eggs {OUTPUT_SENTINEL}"
    llm = ScriptedLlm(first, then=inconsistent_extraction())
    harness = build_harness(llm, ocr_text=f"{COSTCO_OCR}\n{OCR_SENTINEL}")
    graph = harness.process_image().graph
    assert graph is not None
    harness.resolve(require_id(graph.review_id), receipt_draft())
    second = build_harness(ScriptedLlm(extraction(), then=extraction()))
    second.process_text(f"{COSTCO_OCR}\n{OCR_SENTINEL}")

    lines = _lines(capsys.readouterr().out)

    system_prompt = (PROMPT_DIR / "system.md").read_text(encoding="utf-8").strip()
    forbidden = [
        OCR_SENTINEL,
        OUTPUT_SENTINEL,
        "<receipt_data>",
        "<validation_feedback>",
        system_prompt.splitlines()[0][:40],
        LINE3_ISSUE,
        "KS SOURDOUGH",
    ]
    loud = [line for line in lines if str(line.get("level", "")).lower() not in QUIET_LEVELS]
    assert loud, "the run should log something at INFO (e.g. receipt.recorded)"
    for line in loud:
        text = json.dumps(line)
        for needle in forbidden:
            assert needle not in text, (needle, line.get("event"))


def test_ac_a_14_slice_d_vision_and_enrichment_log_no_content(
    make_settings: MakeSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    """The slice D stages log like the others: the enrichment's answer (a product name, tags)
    and the photo never reach a line at INFO or above."""
    configure_logging(make_settings(log_level="DEBUG", log_format="json"))
    answers = [dict(answer) for answer in COSTCO_ENRICHMENTS]
    answers[0]["canonical_name"] = f"Eggs {OUTPUT_SENTINEL}"
    answers[0]["tags"] = ["sentinel-tag-5d1e"]
    vision = printed_extraction()
    llm = ScriptedLlm(inconsistent_extraction(), inconsistent_extraction(), vision, *answers)
    settings = agent_settings(max_vision_attempts=1, enrichment=True)
    harness = build_harness(llm, settings=settings, ocr_text=f"{COSTCO_OCR}\n{OCR_SENTINEL}")
    graph = harness.process_image("sentinel-photo").graph
    assert graph is not None
    assert graph.vision_attempts == 1

    lines = _lines(capsys.readouterr().out)

    loud = [line for line in lines if str(line.get("level", "")).lower() not in QUIET_LEVELS]
    assert loud
    for line in loud:
        text = json.dumps(line)
        for needle in (OCR_SENTINEL, OUTPUT_SENTINEL, "sentinel-tag-5d1e", "sentinel-photo"):
            assert needle not in text, (needle, line.get("event"))
