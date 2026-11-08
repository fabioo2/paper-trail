"""STRETCH S2 (cut together with this file): a leading item code is not part of the product's
name (EA-AC-13; docs/plans/extraction-accuracy.md 2.8).

The enrichment model read ``1281 1% MILK`` as a 1281 ml product. Pinned:
``split_item_code(raw_text) -> (code | None, text)`` in
``paper_trail.domain.rules.normalization``: a leading run of 4-13 digits followed by whitespace
is the code (Costco 4-7 digits, PLU 4, UPC 11-13); anything else is returned unchanged with no
code. The enrichment request's ``line:`` is the text without the code; aliases are unchanged.
"""

import pytest

from paper_trail.domain.rules.normalization import split_item_code
from tests.agent_harness import agent_settings, build_harness
from tests.extractions import enrichment, one_line_extraction
from tests.fakes.llm import ScriptedLlm


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        ("1281 1% MILK", ("1281", "1% MILK")),
        ("1362394 CHOC PISTACH", ("1362394", "CHOC PISTACH")),
        ("4011 BANANAS", ("4011", "BANANAS")),  # a PLU
        ("06038318640 PC BLACK BEANS", ("06038318640", "PC BLACK BEANS")),  # UPC, 11 digits
        ("0060383186409 PC CHICKPEAS", ("0060383186409", "PC CHICKPEAS")),  # 13 digits
        ("1281\t1% MILK", ("1281", "1% MILK")),
    ],
)
def test_ea_ac_13_a_leading_item_code_is_split_off(
    raw_text: str, expected: tuple[str, str]
) -> None:
    assert split_item_code(raw_text) == expected


@pytest.mark.parametrize(
    "raw_text",
    [
        "2% MILK",
        "12 EGGS",  # fewer than 4 digits: a count, not a code
        "540 ML BEANS",
        "12345678901234 TOO LONG",  # 14 digits
        "1281",  # nothing after it
        "1281MILK",  # no whitespace after the digits
        "KS 1281 MILK",  # not leading
        "",
    ],
)
def test_ea_ac_13_anything_else_is_unchanged(raw_text: str) -> None:
    assert split_item_code(raw_text) == (None, raw_text)


def test_ea_ac_13_the_enrichment_request_names_the_line_without_its_code() -> None:
    answer = enrichment("Kirkland Signature 1% Milk 4 L", size_value="4", size_unit="l")
    harness = build_harness(
        ScriptedLlm(one_line_extraction("1281 1% MILK", description="1% Milk"), answer),
        settings=agent_settings(enrichment=True),
    )

    outcome = harness.process_text("1281 1% MILK 8.99")

    assert harness.llm.calls == 2
    final = harness.llm.last_user_message(1)
    data = final[final.index("<receipt_data>") : final.index("</receipt_data>")]
    assert "1% MILK" in data
    assert "1281" not in data
    assert outcome.graph is not None
    product_id = harness.line(outcome.receipt_id, 0).product_id
    assert product_id is not None
    assert harness.product(product_id).size_value == 4
