"""The ``extraction/v2`` and ``vision/v1`` prompt files (EA-AC-10, S3-AC-09).

docs/plans/extraction-accuracy.md 2.5. Pinned:
- ``extraction/v2/``: ``system.md``, ``user.md`` (the same template as v1), ``schema.json`` =
  ``PrintedExtraction.model_json_schema()``, and three examples whose outputs are printed-value
  answers: every store name contains ``SAMPLE``, every item line names a ``SAMPLE`` item, the
  card is ``0000`` or absent. Each converts with ``to_output()`` and adds up.
- ``vision/v1/``: ``system.md``, ``user.md`` without ``{ocr_text}`` and without a data block,
  ``schema.json`` = v2's; no examples.
- The v1 files are unchanged byte for byte (experiment E1 compares against them).
"""

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from paper_trail.agents.prompts import PromptLibrary
from paper_trail.agents.schemas import PrintedExtraction
from paper_trail.domain.enums import LineKind
from paper_trail.domain.rules.arithmetic import check_arithmetic
from tests.extractions import COSTCO_OCR, LINE3_ISSUE
from tests.support import REPO_ROOT

PROMPTS = REPO_ROOT / "src" / "paper_trail" / "agents" / "prompts"
V1 = PROMPTS / "extraction" / "v1"
V2 = PROMPTS / "extraction" / "v2"
VISION = PROMPTS / "vision" / "v1"
EXPECTED_V2_EXAMPLES = (
    "01-sample-discount-multibuy.json",
    "02-sample-weighed-yymmdd.json",
    "03-sample-injection.json",
)
TODAY = date(2026, 11, 8)
NUM_CTX = 8192
OUTPUT_BUDGET_TOKENS = 2048
CHARS_PER_TOKEN = 3

# sha256 of every extraction/v1 file on 2026-11-08, before this change.
V1_FILES = {
    "examples/01-multibuy.json": "07d5d3969b276d0048f34be0458780c8c53bd9441bd24f2448cf41b9fe581a3b",
    "examples/02-weighed.json": "4a5936a318360e9b6c35a92225fbe156034cb73fc2caf23512228d552cb66dd7",
    "examples/03-discount-deposit.json": (
        "40e06352f69ab6df2bd068b03fc260ed1d1b151e60d5265d97bcac047d6f4895"
    ),
    "examples/04-faded-total.json": (
        "24a382018f7c2d7ec5a7ccb0c8e833fa409a9d67219d52ccffef21b64e8b3067"
    ),
    "examples/05-injection.json": (
        "028979f1ce0387c8b3b9403a56ece602286410d7565ef5bc4fccdee357e5102e"
    ),
    "schema.json": "224d40a7959cfc065606f2fc7d981306c3e627c2a3516238a401c3ad9285bc05",
    "system.md": "2395abb63340d96de65a18f81597b2ca528c159da2bc01ba62a08ccd868f5a6c",
    "user.md": "5b3e34e7a7ae6cb3582d62ae91cf2db9be4a6bce2a61566f087eed92a1e28b55",
}


def _v2_examples() -> list[tuple[str, dict[str, Any]]]:
    return [
        (path.name, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted((V2 / "examples").glob("*.json"))
    ]


def test_ea_ac_10_v1_files_are_unchanged() -> None:
    on_disk = {
        path.relative_to(V1).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(V1.rglob("*"))
        if path.is_file()
    }

    assert on_disk == V1_FILES


@pytest.mark.parametrize("directory", [V2, VISION], ids=["extraction-v2", "vision-v1"])
def test_ea_ac_10_schema_files_equal_the_printed_schema(directory: Path) -> None:
    on_disk = json.loads((directory / "schema.json").read_text(encoding="utf-8"))

    assert on_disk == PrintedExtraction.model_json_schema()


def test_ea_ac_10_the_planned_v2_examples_exist() -> None:
    assert tuple(name for name, _ in _v2_examples()) == EXPECTED_V2_EXAMPLES


@pytest.mark.parametrize("name", EXPECTED_V2_EXAMPLES)
def test_ea_ac_10_every_v2_example_converts_and_adds_up(name: str) -> None:
    """Example 01's shared multi-buy amount needs stretch S1: cut S1, change that example."""
    example = json.loads((V2 / "examples" / name).read_text(encoding="utf-8"))

    assert set(example) == {"ocr_text", "output"}
    assert example["ocr_text"].strip()
    output = PrintedExtraction.model_validate_json(json.dumps(example["output"])).to_output()
    draft = output.to_draft(today=TODAY, ocr_text=example["ocr_text"], image_path=None)
    assert check_arithmetic(draft).ok, check_arithmetic(draft).issues


@pytest.mark.parametrize("name", EXPECTED_V2_EXAMPLES)
def test_ea_ac_10_v2_examples_name_no_real_store_item_or_card(name: str) -> None:
    """Evidence 2: a real-looking example (``WALMART``, ``GV PEANUT BUTTER``) was copied."""
    output = json.loads((V2 / "examples" / name).read_text(encoding="utf-8"))["output"]

    assert "SAMPLE" in output["store_name"]
    assert output["payment_last4"] in (None, "0000")
    for line in output["items"]:
        if line["kind"] == LineKind.ITEM.value:
            assert "SAMPLE" in line["raw_text"], line


def test_ea_ac_10_the_v2_examples_show_the_hard_cases() -> None:
    """A trailing-minus amount, a weighed line and a two-digit-year date are shown as printed."""
    outputs = [example["output"] for _, example in _v2_examples()]
    amounts = [line["amount"] for output in outputs for line in output["items"]]
    quantities = [line["quantity"] or "" for output in outputs for line in output["items"]]

    assert any(amount is not None and amount.strip().endswith("-") for amount in amounts)
    assert any("kg" in quantity for quantity in quantities)
    assert "26/10/14" in [output["purchased_on"] for output in outputs]


def test_ea_ac_10_the_injection_example_ignores_the_injected_line() -> None:
    _, example = _v2_examples()[-1]

    assert "ignore" in example["ocr_text"].lower()
    assert "ignore" not in json.dumps(example["output"]).lower()


def test_ea_ac_10_v2_user_template_is_v1s() -> None:
    assert (V2 / "user.md").read_text(encoding="utf-8") == (V1 / "user.md").read_text(
        encoding="utf-8"
    )


def test_s3_ac_09_the_v2_system_prompt_keeps_the_data_rule_and_the_vocabulary() -> None:
    system = (V2 / "system.md").read_text(encoding="utf-8")

    assert "<receipt_data>" in system
    for kind in LineKind:
        assert kind.value in system


def test_ea_ac_10_the_v2_prompt_renders_with_its_examples() -> None:
    prompt = PromptLibrary().load("extraction", "v2")

    messages = prompt.messages(COSTCO_OCR, (LINE3_ISSUE,))

    assert prompt.tag == "extraction/v2"
    roles = [message.role for message in messages]
    assert roles == ["system", *["user", "assistant"] * len(EXPECTED_V2_EXAMPLES), "user"]
    assert "KS ORG EGGS 24CT" in messages[-1].content
    characters = sum(len(message.content) for message in messages)
    assert characters <= (NUM_CTX - OUTPUT_BUDGET_TOKENS) * CHARS_PER_TOKEN


# --- vision/v1 ------------------------------------------------------------------------------------


def test_ea_ac_10_vision_has_no_examples() -> None:
    assert not list((VISION / "examples").glob("*.json"))
    assert PromptLibrary().load("vision", "v1").examples == ()


def test_ea_ac_10_the_vision_user_message_has_no_ocr_text() -> None:
    template = (VISION / "user.md").read_text(encoding="utf-8")
    assert "{ocr_text}" not in template
    assert "{feedback_block}" in template

    messages = PromptLibrary().load("vision", "v1").messages(COSTCO_OCR, ())

    assert [message.role for message in messages] == ["system", "user"]
    assert "<receipt_data>" not in messages[-1].content
    assert all("KS ORG EGGS" not in message.content for message in messages)


def test_ea_ac_10_the_vision_user_message_still_carries_feedback() -> None:
    final = PromptLibrary().load("vision", "v1").messages(COSTCO_OCR, (LINE3_ISSUE,))[-1]

    assert "<validation_feedback>" in final.content
    assert LINE3_ISSUE in final.content


# --- fix round: the rules the real Costco and Superstore runs needed -----------------------------


@pytest.mark.parametrize("directory", [V2, VISION], ids=["extraction-v2", "vision-v1"])
def test_fix_round_both_prompts_state_the_real_run_rules(directory: Path) -> None:
    system = (directory / "system.md").read_text(encoding="utf-8")

    assert "never merge" in system.lower()  # finding 9: repeated identical lines
    assert '"1.5KG"' in system  # finding 7: sizes and counts in the name are not quantities
    assert '"0000392208 / 1362394"' in system  # finding 8: item references are discounts
    assert "right edge" in system  # finding 10: the amount is the number at the right edge
    assert '"$5.99 ea or 2/$10.00"' in system  # ... never a price inside offer text
    assert "except the last" in system  # review finding 5: the multi-buy wording
    # blind test: the tax amount, never the taxable base or the rate
    assert '"0.84" in "HST 13% 6.49 @ 13.000% 0.84"' in system
    assert "never the taxable base or the rate" in system
    assert (  # a detail line is part of the product above it, never its own item
        'A detail line ("N @ price" or a weight) belongs to the product printed above it;'
        " it is never an item of its own."
    ) in system


@pytest.mark.parametrize("directory", [V2, VISION], ids=["extraction-v2", "vision-v1"])
def test_review_3_an_n_at_multi_buy_price_is_not_listed_as_promotion_text(directory: Path) -> None:
    """Example 01 uses "2 @ 2/$5.00": "2/$5.00" after "N @" is a unit price, not an offer."""
    system = (directory / "system.md").read_text(encoding="utf-8")
    promotion = next(line for line in system.splitlines() if "offer" in line.lower())

    assert '"2/$5.00"' not in promotion
    assert '"N @"' in promotion
