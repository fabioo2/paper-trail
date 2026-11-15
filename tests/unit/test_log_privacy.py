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
    COSTCO_OCR,
    L3,
    LINE3_ISSUE,
    LOBLAWS,
    extraction,
    inconsistent_extraction,
    printed_extraction,
)
from tests.factories import receipt_draft
from tests.fakes.catalog import TEST_APPLE, FakeCatalog
from tests.fakes.llm import ScriptedLlm
from tests.support import REPO_ROOT, MakeSettings, require_id

OCR_SENTINEL = "SENTINEL-OCR-7F3A"
OUTPUT_SENTINEL = "SENTINEL-LLM-91C2"
PROMPT_DIR = REPO_ROOT / "src" / "paper_trail" / "agents" / "prompts" / "extraction" / "v1"
QUIET_LEVELS = {"debug", "notset"}
# What a catalog.* event may carry besides structlog's own keys: codes and ids, never text.
CATALOG_LOG_KEYS = {
    "event",
    "level",
    "timestamp",
    "logger",
    "scheme",
    "store_key",
    "code",
    "product_id",
    "existing_product_id",
    "refused_product_id",
    "path",
    "reason",
}


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


def test_ac_a_14_slice_d_vision_and_catalog_log_no_content(
    make_settings: MakeSettings, capsys: pytest.CaptureFixture[str]
) -> None:
    """PC-TC-84: the vision answer, the line's raw text, the PLU entry's name and the product it
    names never reach a line at INFO or above; ``catalog.*`` events carry codes and ids only."""
    configure_logging(make_settings(log_level="DEBUG", log_format="json"))
    vision = printed_extraction(
        store_name=LOBLAWS,
        subtotal=None,
        taxes=[],
        total="4.99",
        items=[
            {
                "raw_text": L3,
                "description": f"Apple {OUTPUT_SENTINEL}",
                "quantity": "1",
                "unit_price": "4.99",
                "amount": "4.99",
                "kind": "item",
            }
        ],
    )
    llm = ScriptedLlm(inconsistent_extraction(), inconsistent_extraction(), vision)
    settings = agent_settings(max_vision_attempts=1, enrichment=True)
    harness = build_harness(
        llm,
        settings=settings,
        ocr_text=f"{COSTCO_OCR}\n{OCR_SENTINEL}",
        catalog=FakeCatalog({"3101": TEST_APPLE}),
    )
    graph = harness.process_image("sentinel-photo").graph
    assert graph is not None
    assert graph.vision_attempts == 1
    (item,) = harness.receipt(graph.receipt_id).items
    assert harness.product(require_id(item.product_id)).category is not None  # described

    lines = _lines(capsys.readouterr().out)

    loud = [line for line in lines if str(line.get("level", "")).lower() not in QUIET_LEVELS]
    assert loud
    forbidden = (OCR_SENTINEL, OUTPUT_SENTINEL, "sentinel-photo", "Test Apple", "TST APPLE")
    for line in loud:
        text = json.dumps(line)
        for needle in forbidden:
            assert needle not in text, (needle, line.get("event"))
    for line in lines:
        if str(line.get("event", "")).startswith("catalog."):
            assert set(line) <= CATALOG_LOG_KEYS, line
