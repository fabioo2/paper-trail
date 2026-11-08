"""The JSON import format of `paper-trail receipts import` (S1-AC-24, PLAN 3.8).

Pinned (docs/tests/stage1.md): ``ReceiptImport.model_validate(data).to_draft()`` builds the
ReceiptDraft; cents fields are strict integers (no floats, no strings); ``purchased_on`` is
optional; ``description`` defaults to ``raw_text``; positions are the list order; ocr_text and
image_path are None.
"""

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from paper_trail.domain.enums import Category, LineKind
from paper_trail.domain.money import Money
from paper_trail.interfaces.cli.schemas import ReceiptImport
from tests.support import REPO_ROOT

COSTCO_SIMPLE = REPO_ROOT / "tests" / "data" / "receipts" / "costco_simple.json"


def _minimal(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "store_name": "Costco",
        "total_cents": 899,
        "items": [{"raw_text": "KS ORG EGGS 24CT", "line_total_cents": 899}],
    }
    data.update(overrides)
    return data


def _error_locations(error: ValidationError) -> list[str]:
    return [".".join(str(part) for part in detail["loc"]) for detail in error.errors()]


def test_s1_ac_24_minimal_import_uses_the_defaults() -> None:
    """S1-AC-24 / PLAN 3.8: currency CAD, quantity 1, kind item, description = raw_text."""
    draft = ReceiptImport.model_validate(_minimal()).to_draft()

    assert draft.store_name == "Costco"
    assert draft.purchased_on is None
    assert draft.currency == "CAD"
    assert (draft.subtotal, draft.tax, draft.total) == (None, None, Money(899, "CAD"))
    assert (draft.payment_last4, draft.ocr_text, draft.image_path) == (None, None, None)
    (item,) = draft.items
    assert item.position == 0
    assert item.kind is LineKind.ITEM
    assert item.raw_text == item.description == "KS ORG EGGS 24CT"
    assert item.quantity == Decimal("1")
    assert item.unit_price is None
    assert item.line_total == Money(899, "CAD")
    assert (item.product_id, item.category, item.id) == (None, None, None)
    assert dict(draft.product_names) == {}


def test_s1_ac_24_full_import_maps_every_field() -> None:
    """S1-AC-24: every field of the import format reaches the draft, in the receipt currency."""
    data = {
        "store_name": "Walmart Supercentre 3150",
        "purchased_on": "2026-11-02",
        "currency": "USD",
        "subtotal_cents": 1047,
        "tax_cents": 52,
        "total_cents": 1099,
        "payment_last4": "4242",
        "items": [
            {"raw_text": "BANANAS", "description": "Bananas", "quantity": "1.36",
             "unit_price_cents": 150, "line_total_cents": 204, "product": "Bananas",
             "category": "produce"},
            {"raw_text": "GV MILK 4L", "line_total_cents": 943, "kind": "item",
             "product": "Great Value Milk 4 L"},
            {"raw_text": "ROLLBACK", "line_total_cents": -100, "kind": "discount"},
        ],
    }  # fmt: skip

    draft = ReceiptImport.model_validate(data).to_draft()

    assert draft.purchased_on == date(2026, 11, 2)
    assert (draft.subtotal, draft.tax, draft.total) == (
        Money(1047, "USD"),
        Money(52, "USD"),
        Money(1099, "USD"),
    )
    assert draft.payment_last4 == "4242"
    bananas, milk, rollback = draft.items
    assert [item.position for item in draft.items] == [0, 1, 2]
    assert bananas.quantity == Decimal("1.36")
    assert str(bananas.quantity) == "1.36"
    assert bananas.unit_price == Money(150, "USD")
    assert bananas.category is Category.PRODUCE
    assert milk.description == "GV MILK 4L"
    assert rollback.kind is LineKind.DISCOUNT
    assert rollback.line_total == Money(-100, "USD")
    assert dict(draft.product_names) == {0: "Bananas", 1: "Great Value Milk 4 L"}


def test_s1_ac_24_the_sample_file_is_a_valid_import() -> None:
    """S1-AC-24: tests/data/receipts/costco_simple.json parses into a consistent-looking draft."""
    data = ReceiptImport.model_validate_json(COSTCO_SIMPLE.read_text(encoding="utf-8"))

    draft = data.to_draft()

    assert draft.store_name
    assert draft.items
    assert draft.product_names


@pytest.mark.parametrize(
    ("data", "field"),
    [
        (_minimal(colour="blue"), "colour"),
        (_minimal(items=[{"raw_text": "X", "line_total_cents": 1, "sku": "1"}]), "items.0.sku"),
        (_minimal(total_cents=8.99), "total_cents"),
        (_minimal(total_cents="899"), "total_cents"),
        (_minimal(total_cents=899.0), "total_cents"),
        (_minimal(subtotal_cents=True), "subtotal_cents"),
        (_minimal(items=[{"raw_text": "X", "line_total_cents": 1.5}]), "items.0.line_total_cents"),
        (
            _minimal(items=[{"raw_text": "X", "line_total_cents": 1, "unit_price_cents": "1"}]),
            "items.0.unit_price_cents",
        ),
        (
            _minimal(items=[{"raw_text": "X", "line_total_cents": 1, "kind": "refund"}]),
            "items.0.kind",
        ),
        (
            _minimal(items=[{"raw_text": "X", "line_total_cents": 1, "category": "toys"}]),
            "items.0.category",
        ),
        (_minimal(purchased_on="06/11/2026"), "purchased_on"),
        (_minimal(payment_last4="42"), "payment_last4"),
        (_minimal(currency="cad"), "currency"),
        ({"store_name": "Costco", "items": []}, "total_cents"),
    ],
)
def test_s1_ac_24_invalid_imports_name_the_field(data: dict[str, Any], field: str) -> None:
    """S1-AC-24: unknown fields and non-integer cents are rejected, naming the field."""
    with pytest.raises(ValidationError) as caught:
        ReceiptImport.model_validate(data)

    assert field in _error_locations(caught.value)


@pytest.mark.parametrize("quantity", ["0", "-1", "abc", "NaN", 1.5, 2])
def test_s1_ac_24_quantity_must_be_a_positive_decimal(quantity: object) -> None:
    """S1-AC-24 / S1-AC-02: a positive decimal string; numbers, NaN and zero are invalid."""
    data = _minimal(items=[{"raw_text": "X", "line_total_cents": 1, "quantity": quantity}])

    with pytest.raises(ValidationError) as caught:
        ReceiptImport.model_validate(data)

    assert "items.0.quantity" in _error_locations(caught.value)
