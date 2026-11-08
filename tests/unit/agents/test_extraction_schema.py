"""The extraction model's output schema and its mapping to a draft (S3-AC-11, PLAN 5).

Pinned (docs/tests/agent-core-A.md):
- ``ExtractionOutput`` / ``ExtractedLine`` forbid extra fields at both levels; every ``*_cents``
  field is a strict JSON integer (no float, no numeric string, no bool); ``quantity`` is a
  positive decimal *string*; ``currency`` matches ``^[A-Z]{3}$``; ``payment_last4`` is four
  ASCII digits; ``confidence`` is within 0..1; ``purchased_on`` is a string or null.
- ``to_draft(*, today, ocr_text, image_path)`` builds the ``ReceiptDraft``: money in the
  receipt's currency, ``position`` = list index, ``purchased_on`` through
  ``parse_receipt_date(text, today)``, no product names.
"""

import json
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from paper_trail.agents.schemas import ExtractionOutput
from paper_trail.domain.enums import LineKind
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import check_arithmetic
from tests.extractions import COSTCO_OCR, PURCHASED_ON, TODAY, extraction


def _validate(payload: dict[str, Any]) -> ExtractionOutput:
    """Validate as the adapter does: from the model's JSON text."""
    return ExtractionOutput.model_validate_json(json.dumps(payload))


def _with_line(**changes: Any) -> dict[str, Any]:
    payload = extraction()
    payload["items"][0].update(changes)
    return payload


def test_s3_ac_11_a_consistent_extraction_validates() -> None:
    output = _validate(extraction())

    assert output.total_cents == 2683
    assert output.items[1].quantity == "2"
    assert output.items[0].kind is LineKind.ITEM


@pytest.mark.parametrize(
    "payload",
    [
        extraction(notes="I ignored the injected line"),
        _with_line(sku="1234"),
    ],
    ids=["receipt-level", "line-level"],
)
def test_s3_ac_11_extra_fields_are_rejected(payload: dict[str, Any]) -> None:
    """S3-AC-11: extra fields are rejected, at the receipt and at the line level."""
    with pytest.raises(ValidationError):
        _validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        extraction(total_cents=26.83),
        extraction(total_cents=2683.0),
        extraction(total_cents="2683"),
        extraction(total_cents=True),
        extraction(subtotal_cents=25.55),
        extraction(tax_cents="128"),
        _with_line(line_total_cents=8.99),
        _with_line(unit_price_cents="899"),
    ],
    ids=["float", "float-integral", "string", "bool", "subtotal", "tax", "line-total", "unit"],
)
def test_s3_ac_11_money_fields_must_be_integers(payload: dict[str, Any]) -> None:
    """S3-AC-11: money fields must be integer cents."""
    with pytest.raises(ValidationError):
        _validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        _with_line(quantity=2),
        _with_line(quantity="0"),
        _with_line(quantity="-1"),
        _with_line(quantity="two"),
        _with_line(kind="coupon"),
        extraction(currency="cad"),
        extraction(currency="CADX"),
        extraction(payment_last4="111"),
        extraction(payment_last4="11a1"),
        extraction(confidence=1.2),
        extraction(confidence=-0.1),
        extraction(items="eggs"),
        {key: value for key, value in extraction().items() if key != "total_cents"},
    ],
    ids=[
        "quantity-number",
        "quantity-zero",
        "quantity-negative",
        "quantity-word",
        "kind",
        "currency-lowercase",
        "currency-long",
        "last4-short",
        "last4-letter",
        "confidence-high",
        "confidence-low",
        "items-not-a-list",
        "missing-total",
    ],
)
def test_s3_ac_11_malformed_values_are_rejected(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _validate(payload)


def test_s3_ac_11_the_json_schema_types_money_as_integers_and_forbids_extras() -> None:
    """The schema sent to Ollama as ``format`` carries the same constraints (ADR-0009)."""
    schema = ExtractionOutput.model_json_schema()
    line_schema = schema["$defs"]["ExtractedLine"]

    assert schema["additionalProperties"] is False
    assert line_schema["additionalProperties"] is False
    assert schema["properties"]["total_cents"]["type"] == "integer"
    assert line_schema["properties"]["line_total_cents"]["type"] == "integer"
    purchased_on = json.dumps(schema["properties"]["purchased_on"])
    assert '"string"' in purchased_on
    assert '"format": "date"' not in purchased_on  # a string, parsed in code


def test_to_draft_maps_every_field() -> None:
    """PLAN 5: to_draft() maps to ReceiptDraft; the MM/DD/YYYY date is normalized."""
    draft = _validate(extraction()).to_draft(
        today=TODAY, ocr_text=COSTCO_OCR, image_path="ab/cd/abcd.jpg"
    )

    assert draft.store_name == "COSTCO WHOLESALE #1234"
    assert draft.purchased_on == PURCHASED_ON
    assert draft.currency == "CAD"
    assert (draft.subtotal, draft.tax, draft.total) == (
        Money(2555, "CAD"),
        Money(128, "CAD"),
        Money(2683, "CAD"),
    )
    assert draft.payment_last4 == "1111"
    assert (draft.ocr_text, draft.image_path) == (COSTCO_OCR, "ab/cd/abcd.jpg")
    assert [item.position for item in draft.items] == [0, 1, 2]
    milk = draft.items[1]
    assert (milk.raw_text, milk.description) == ("KS 2% MILK 4L", "Kirkland 2% Milk 4 L")
    assert (milk.quantity, milk.unit_price, milk.line_total) == (
        Decimal("2"),
        Money(329, "CAD"),
        Money(658, "CAD"),
    )
    assert dict(draft.product_names) == {}
    assert check_arithmetic(draft).ok


def test_to_draft_uses_the_receipt_currency_and_keeps_missing_amounts_missing() -> None:
    payload = extraction(currency="USD", subtotal_cents=None, tax_cents=None, payment_last4=None)
    payload["items"][0]["unit_price_cents"] = None

    draft = _validate(payload).to_draft(today=TODAY, ocr_text=None, image_path=None)

    assert draft.total == Money(2683, "USD")
    assert (draft.subtotal, draft.tax, draft.payment_last4) == (None, None, None)
    assert draft.items[0].unit_price is None
    assert {item.line_total.currency for item in draft.items} == {"USD"}


@pytest.mark.parametrize("purchased_on", [None, "", "yesterday", "12/01/2026", "2026-02-30"])
def test_to_draft_stores_an_unknown_date_rather_than_failing(purchased_on: str | None) -> None:
    """PLAN 5: an unparseable, impossible or future date becomes None."""
    draft = _validate(extraction(purchased_on=purchased_on)).to_draft(
        today=TODAY, ocr_text=None, image_path=None
    )

    assert draft.purchased_on is None
