"""STRETCH S2 (cut together with this file): a leading item code is not part of the product's
name (EA-AC-13; docs/plans/extraction-accuracy.md 2.8).

The enrichment model read ``1281 1% MILK`` as a 1281 ml product. Pinned:
``split_item_code(raw_text) -> (code | None, text)`` in
``paper_trail.domain.rules.normalization``: a leading run of 4-14 digits followed by whitespace
is the code (Costco 4-7 digits, PLU 4, UPC 11-13, GTIN-14 since barcode-lookup PC-TC-06);
anything else is returned unchanged with no code. Aliases are unchanged. The enrichment request
this file pinned is gone (no model in enrichment); the printed name owns it (PC-TC-08).
"""

import pytest

from paper_trail.domain.rules.normalization import split_item_code


@pytest.mark.parametrize(
    ("raw_text", "expected"),
    [
        ("1281 1% MILK", ("1281", "1% MILK")),
        ("1362394 CHOC PISTACH", ("1362394", "CHOC PISTACH")),
        ("4011 BANANAS", ("4011", "BANANAS")),  # a PLU
        ("06038318640 PC BLACK BEANS", ("06038318640", "PC BLACK BEANS")),  # UPC, 11 digits
        ("0060383186409 PC CHICKPEAS", ("0060383186409", "PC CHICKPEAS")),  # 13 digits
        ("00012345600012 X", ("00012345600012", "X")),  # GTIN-14 (barcode-lookup PC-TC-06)
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
        "123456789012345 TOO LONG",  # 15 digits
        "1281",  # nothing after it
        "1281MILK",  # no whitespace after the digits
        "KS 1281 MILK",  # not leading
        "",
    ],
)
def test_ea_ac_13_anything_else_is_unchanged(raw_text: str) -> None:
    assert split_item_code(raw_text) == (None, raw_text)


ITEM_CODE_MARKERS = [
    pytest.param("(2) 1362394 CHOC PISTACH", ("1362394", "CHOC PISTACH"), id="TC-08"),
    pytest.param("(10) 1543210 GOLD KIWI", ("1543210", "GOLD KIWI"), id="TC-08-two-digits"),
    pytest.param(
        "(1)05700001285 CRV CHKKN", ("05700001285", "CRV CHKKN"), id="TC-08-no-space-after-marker"
    ),
    pytest.param("(2) GOLD KIWI", (None, "(2) GOLD KIWI"), id="TC-10-marker-without-code"),
    pytest.param("(a) 1362394 X", (None, "(a) 1362394 X"), id="TC-11-not-a-number"),
    pytest.param("(2 1362394 X", (None, "(2 1362394 X"), id="TC-11-unclosed"),
    pytest.param("(2) 123 X", (None, "(2) 123 X"), id="TC-11-code-too-short"),
    pytest.param("(123) 1362394 X", (None, "(123) 1362394 X"), id="TC-11-three-digit-marker"),
]


@pytest.mark.parametrize(("raw_text", "expected"), ITEM_CODE_MARKERS)
def test_a_leading_quantity_marker_is_dropped_with_the_code(
    raw_text: str, expected: tuple[str | None, str]
) -> None:
    assert split_item_code(raw_text) == expected
