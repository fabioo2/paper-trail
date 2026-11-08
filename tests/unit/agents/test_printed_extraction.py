"""The printed-value answer and its conversion (EA-AC-04, EA-AC-05, ADR-0017).

docs/plans/extraction-accuracy.md 2.1: ``extraction/v2`` and ``vision/v1`` answer
``PrintedExtraction`` (amounts, quantities, unit prices, taxes and the date as printed strings);
``to_output()`` converts it into the ``ExtractionOutput`` every later step already takes.

Pinned:
- ``PrintedLine(raw_text, description, quantity, unit_price, amount, kind)`` and
  ``PrintedExtraction(store_name, purchased_on, currency, subtotal, taxes, total,
  payment_last4, items, confidence)`` in ``paper_trail.agents.schemas`` forbid extra fields.
- ``to_output()``: amounts through ``parse_amount``, the sign through ``line_kind``, quantity
  null -> 1, the tax is the sum of ``taxes``, the date text is kept for ``to_draft`` to parse.
  Every unreadable field is collected into ONE ``PrintedValueError`` naming each field (``line N
  amount``, 1-based like the arithmetic issues) and quoting its printed text.
- ``ExtractionOutput.to_output()`` returns the output itself; ``ANSWER_MODELS`` maps a prompt
  id (without ``+noex``) to the answer model.

Fixtures: ``tests/data/extraction/costco_printed.json`` is the 2026-11-05 Costco photo's answer as
a good model would print it (22 lines, synthetic item codes and card).
"""

import json
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from paper_trail.agents.schemas import ANSWER_MODELS, ExtractionOutput, PrintedExtraction
from paper_trail.domain.enums import LineKind
from paper_trail.domain.errors import PrintedValueError
from paper_trail.domain.rules.arithmetic import check_arithmetic
from tests.extractions import (
    PURCHASED_ON,
    TODAY,
    extraction,
    printed_extraction,
    printed_fixture,
)

EA_TODAY = date(2026, 11, 8)


def _validate(payload: dict[str, Any]) -> PrintedExtraction:
    """Validate as the adapter does: from the model's JSON text."""
    return PrintedExtraction.model_validate_json(json.dumps(payload))


def _convert(payload: dict[str, Any]) -> ExtractionOutput:
    return _validate(payload).to_output()


# --- the schema ----------------------------------------------------------------------------------


def test_the_default_receipt_converts_to_its_integer_cents_answer() -> None:
    output = _convert(printed_extraction())

    expected = ExtractionOutput.model_validate(extraction())
    assert output.receipt_json() | {"items": None} == expected.receipt_json() | {"items": None}
    assert output.confidence == expected.confidence
    for converted, wanted in zip(output.items, expected.items, strict=True):
        assert Decimal(converted.quantity) == Decimal(wanted.quantity)
        assert (converted.raw_text, converted.description, converted.kind) == (
            wanted.raw_text,
            wanted.description,
            wanted.kind,
        )
        assert (converted.unit_price_cents, converted.line_total_cents) == (
            wanted.unit_price_cents,
            wanted.line_total_cents,
        )
    draft = output.to_draft(today=TODAY, ocr_text=None, image_path=None)
    assert draft.purchased_on == PURCHASED_ON


@pytest.mark.parametrize(
    "payload",
    [
        printed_extraction(notes="extra"),
        printed_extraction(subtotal_cents=2555),
        printed_extraction(items=[{**printed_extraction()["items"][0], "line_total_cents": 899}]),
    ],
    ids=["receipt-extra", "cents-field", "line-extra"],
)
def test_extra_fields_are_rejected(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _validate(payload)


def test_amounts_are_strings_in_the_schema() -> None:
    """The model never writes a number it has to compute: no integer amount is accepted."""
    with pytest.raises(ValidationError):
        _validate(printed_extraction(total=2683))


def test_the_v1_answer_converts_to_itself() -> None:
    output = ExtractionOutput.model_validate(extraction())

    assert output.to_output() == output


def test_answer_models_per_prompt() -> None:
    assert ANSWER_MODELS["extraction/v1"] is ExtractionOutput
    assert ANSWER_MODELS["extraction/v2"] is PrintedExtraction
    assert ANSWER_MODELS["vision/v1"] is PrintedExtraction
    assert not any(key.endswith("+noex") for key in ANSWER_MODELS)


# --- EA-AC-04: the Costco photo, as printed -------------------------------------------------------


def test_ea_ac_04_the_costco_answer_converts_and_adds_up() -> None:
    output = _convert(printed_fixture("costco_printed"))

    assert len(output.items) == 22
    assert (output.subtotal_cents, output.tax_cents, output.total_cents) == (18916, 1403, 20319)
    draft = output.to_draft(today=EA_TODAY, ocr_text=None, image_path=None)
    report = check_arithmetic(draft)
    assert report.ok, report.issues
    assert draft.purchased_on == date(2026, 11, 5)


def test_ea_ac_04_every_minus_line_is_a_negative_discount() -> None:
    """The seven ``4.00-`` lines were read as items (evidence 3b) and ``MULTIPLE 6.00-`` as a
    discount: all eight are DISCOUNT lines with negative totals, nothing else is."""
    output = _convert(printed_fixture("costco_printed"))

    discounts = [line for line in output.items if line.kind is LineKind.DISCOUNT]
    assert sorted(line.line_total_cents for line in discounts) == [-600] + [-400] * 7
    assert all(
        line.line_total_cents > 0 for line in output.items if line.kind is not LineKind.DISCOUNT
    )


def test_ea_ac_04_tax_flags_and_null_quantities() -> None:
    output = _convert(printed_fixture("costco_printed"))

    ramen = output.items[1]
    assert ramen.line_total_cents == 2019  # printed "20.19 A"
    assert Decimal(ramen.quantity) == 1  # no quantity printed
    assert ramen.unit_price_cents is None


def test_ea_ac_04_the_tax_is_the_sum_of_the_printed_taxes() -> None:
    payload = printed_fixture("costco_printed")
    payload["taxes"] = ["5.21", "8.82", "0.00"]

    assert _convert(payload).tax_cents == 1403


def test_ea_ac_06_a_two_digit_year_date_converts_relative_to_today() -> None:
    """The Superstore photo's ``26/11/02`` (read as 2002-11-26 before), on 2026-11-08."""
    payload = printed_fixture("costco_printed")
    payload["purchased_on"] = "26/11/02"

    draft = _convert(payload).to_draft(today=EA_TODAY, ocr_text=None, image_path=None)

    assert draft.purchased_on == date(2026, 11, 2)


# --- EA-AC-05: unreadable printed values ---------------------------------------------------------


def test_ea_ac_05_the_10x_answer_is_an_error_naming_the_line_and_field() -> None:
    """Vision returned 1.29 as 1290 (evidence 3a): written without its cents, it is refused."""
    payload = printed_extraction()
    payload["items"][2]["amount"] = "1290"

    with pytest.raises(PrintedValueError) as raised:
        _convert(payload)

    message = str(raised.value)
    assert "line 3" in message
    assert "amount" in message
    assert '"1290"' in message


def test_ea_ac_05_every_bad_field_is_listed_in_one_error() -> None:
    payload = printed_extraction(total="abc", subtotal="25.5")
    payload["items"][0]["amount"] = "1290"
    payload["items"][1]["quantity"] = "0"

    with pytest.raises(PrintedValueError) as raised:
        _convert(payload)

    message = str(raised.value)
    for printed in ('"abc"', '"25.5"', '"1290"', '"0"'):
        assert printed in message
    for field in ("total", "subtotal", "line 1", "line 2"):
        assert field in message


def test_ea_ac_05_a_bad_tax_is_named() -> None:
    payload = printed_extraction(taxes=["1.28", "12"])

    with pytest.raises(PrintedValueError) as raised:
        _convert(payload)

    assert "tax" in str(raised.value)
    assert '"12"' in str(raised.value)


def test_an_unreadable_unit_price_is_dropped_not_an_error() -> None:
    """The unit price is optional and only feeds the line check: unreadable, it is dropped."""
    payload = printed_extraction()
    payload["items"][0]["unit_price"] = "???"
    payload["items"][0]["quantity"] = None

    output = _convert(payload)

    assert output.items[0].line_total_cents == 899
    draft = output.to_draft(today=TODAY, ocr_text=None, image_path=None)
    assert all(issue.position != 0 for issue in check_arithmetic(draft).issues)


# --- fix round: real-run findings 7 and 8 (the owner's Costco photo, vision first) ---------------


def _costco_line(raw_text: str, amount: str, **fields: Any) -> dict[str, Any]:
    line = {
        "raw_text": raw_text,
        "description": raw_text.title(),
        "quantity": None,
        "unit_price": None,
        "amount": amount,
        "kind": "item",
    }
    return line | fields


def _one_receipt(lines: list[dict[str, Any]], total: str) -> dict[str, Any]:
    return printed_extraction(subtotal=None, taxes=[], total=total, items=lines)


def test_real_run_7_a_size_in_the_name_is_not_a_quantity() -> None:
    output = _convert(
        _one_receipt(
            [
                _costco_line("1793017 KIMCHI 1.5KG", "10.99", quantity="1.5KG", unit_price="10.99"),
                _costco_line("245504 DOUGHNUTS 15", "10.99", quantity="15", unit_price="10.99"),
            ],
            total="21.98",
        )
    )

    assert [Decimal(line.quantity) for line in output.items] == [1, 1]
    draft = output.to_draft(today=TODAY, ocr_text=None, image_path=None)
    assert check_arithmetic(draft).ok, check_arithmetic(draft).issues


def test_real_run_8_an_unsigned_item_reference_is_a_negative_discount() -> None:
    output = _convert(
        _one_receipt(
            [
                _costco_line("1362394 CHOC PISTACH", "16.99"),
                _costco_line("0000392208 / 1362394", "4.00"),
                _costco_line("1700334 PJ", "29.99"),
                _costco_line("0000398549 / MULTIPLE", "6.00"),
            ],
            total="36.98",
        )
    )

    assert [(line.kind, line.line_total_cents) for line in output.items] == [
        (LineKind.ITEM, 1699),
        (LineKind.DISCOUNT, -400),
        (LineKind.ITEM, 2999),
        (LineKind.DISCOUNT, -600),
    ]


def test_real_run_a_tax_read_where_subtotal_equals_total_is_zero() -> None:
    """Superstore: subtotal 99.40 == total 99.40, yet vision returned taxes ["8.99 H"]."""
    payload = printed_extraction(subtotal="26.83", taxes=["8.99 H"], total="26.83")
    payload["items"][0]["amount"] = "10.27"
    payload["items"][0]["unit_price"] = "10.27"

    output = _convert(payload)

    assert output.tax_cents == 0
    draft = output.to_draft(today=TODAY, ocr_text=None, image_path=None)
    assert check_arithmetic(draft).ok, check_arithmetic(draft).issues


# --- blind test 2026-11-08: tax values copied with their base and rate ---------------------------


@pytest.mark.parametrize(
    ("taxes", "notes"),
    [
        (["0.84 13%"], []),
        (["H=HST 13% 6.49 @ 13.000% 0.84"], []),
        (["6.49 @ 13.000%"], ["tax 0.84 computed from 6.49 @ 13.000%"]),
    ],
)
def test_blind_test_the_tax_amount_is_read_from_the_tax_line(
    taxes: list[str], notes: list[str]
) -> None:
    payload = printed_extraction(subtotal="25.55", taxes=taxes, total="26.39")
    answer = _validate(payload)

    assert answer.to_output().tax_cents == 84
    assert answer.notes() == notes
