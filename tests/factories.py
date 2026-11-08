"""Builders for domain objects (PLAN 1.7): plain functions with keyword overrides.

Every builder returns a valid object by default. The defaults describe one consistent receipt
(``receipt_draft()`` passes ``check_arithmetic``), so a test only spells out what it is about.
"""

from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CorrectionKind,
    LineKind,
    ProcessingStage,
    ReceiptStatus,
    Unit,
)
from paper_trail.domain.models import (
    Correction,
    LineItem,
    PriceObservation,
    Product,
    ProductAlias,
    Receipt,
    ReceiptDraft,
)
from paper_trail.domain.money import Money
from tests.fakes.clock import DEFAULT_START

CREATED_AT = DEFAULT_START
PURCHASED_ON = date(2026, 11, 1)
STORE_NAME = "COSTCO WHOLESALE #1234"
STORE_KEY = "costco"

# A valid test card number (Visa test range); never a real card.
TEST_PAN = "4111 1111 1111 1111"
MASKED_TEST_PAN = "**** **** **** 1111"


def cad(amount_cents: int) -> Money:
    return Money(amount_cents, "CAD")


def line_item(**overrides: Any) -> LineItem:
    """An ITEM line: 1 x 8.99 = 8.99."""
    values: dict[str, Any] = {
        "position": 0,
        "kind": LineKind.ITEM,
        "raw_text": "KS ORG EGGS 24CT",
        "description": "Kirkland Organic Eggs 24 ct",
        "quantity": Decimal("1"),
        "unit_price": cad(899),
        "line_total": cad(899),
    }
    values.update(overrides)
    return LineItem(**values)


def default_items() -> tuple[LineItem, ...]:
    """Eggs 1 x 8.99 and milk 2 x 3.29; together 15.57."""
    return (
        line_item(),
        line_item(
            position=1,
            raw_text="KS 2% MILK 4L",
            description="Kirkland 2% Milk 4 L",
            quantity=Decimal("2"),
            unit_price=cad(329),
            line_total=cad(658),
            category=Category.DAIRY_EGGS,
        ),
    )


def receipt_draft(**overrides: Any) -> ReceiptDraft:
    """A consistent draft: items 15.57 = subtotal, + tax 0.78 = total 16.35."""
    values: dict[str, Any] = {
        "store_name": STORE_NAME,
        "purchased_on": PURCHASED_ON,
        "currency": "CAD",
        "subtotal": cad(1557),
        "tax": cad(78),
        "total": cad(1635),
        "items": default_items(),
        "payment_last4": "1111",
        "ocr_text": "COSTCO WHOLESALE #1234\nKS ORG EGGS 24CT 8.99\nKS 2% MILK 4L 6.58",
        "image_path": None,
    }
    values.update(overrides)
    return ReceiptDraft(**values)


def draft_with(
    items: Sequence[LineItem],
    *,
    subtotal: int | None,
    tax: int | None,
    total: int,
    currency: str = "CAD",
) -> ReceiptDraft:
    """A draft with the given lines and totals (in cents of ``currency``)."""
    return receipt_draft(
        items=tuple(items),
        currency=currency,
        subtotal=None if subtotal is None else Money(subtotal, currency),
        tax=None if tax is None else Money(tax, currency),
        total=Money(total, currency),
    )


def receipt(**overrides: Any) -> Receipt:
    """An unsaved, COMPLETED receipt with the default items."""
    values: dict[str, Any] = {
        "id": None,
        "status": ReceiptStatus.COMPLETED,
        "stage": ProcessingStage.DONE,
        "store_name": STORE_NAME,
        "store_key": STORE_KEY,
        "purchased_on": PURCHASED_ON,
        "currency": "CAD",
        "subtotal": cad(1557),
        "tax": cad(78),
        "total": cad(1635),
        "payment_last4": "1111",
        "ocr_text": "COSTCO WHOLESALE #1234",
        "image_path": None,
        "items": default_items(),
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
    }
    values.update(overrides)
    return Receipt(**values)


def product(**overrides: Any) -> Product:
    values: dict[str, Any] = {
        "id": None,
        "canonical_name": "Kirkland Signature Organic Eggs",
        "brand": "Kirkland Signature",
        "size_value": Decimal("24"),
        "size_unit": Unit.EACH,
        "category": Category.DAIRY_EGGS,
        "tags": ("organic", "free-run"),
        "created_at": CREATED_AT,
    }
    values.update(overrides)
    return Product(**values)


def product_alias(product_id: int, **overrides: Any) -> ProductAlias:
    values: dict[str, Any] = {
        "id": None,
        "product_id": product_id,
        "alias": "ks org eggs 24ct",
        "store_key": STORE_KEY,
        "source": AliasSource.IMPORT,
        "created_at": CREATED_AT,
    }
    values.update(overrides)
    return ProductAlias(**values)


def price_observation(
    *, product_id: int, receipt_id: int, line_item_id: int, **overrides: Any
) -> PriceObservation:
    values: dict[str, Any] = {
        "id": None,
        "product_id": product_id,
        "receipt_id": receipt_id,
        "line_item_id": line_item_id,
        "store_key": STORE_KEY,
        "observed_on": PURCHASED_ON,
        "quantity": Decimal("1"),
        "unit_price": cad(899),
        "line_total": cad(899),
    }
    values.update(overrides)
    return PriceObservation(**values)


def correction(**overrides: Any) -> Correction:
    values: dict[str, Any] = {
        "id": None,
        "kind": CorrectionKind.CATEGORY,
        "subject": "ks org eggs 24ct",
        "old_value": "pantry",
        "new_value": "dairy_eggs",
        "store_key": STORE_KEY,
        "receipt_id": None,
        "line_item_id": None,
        "created_at": CREATED_AT,
    }
    values.update(overrides)
    return Correction(**values)


def at(minutes: int) -> datetime:
    """A UTC timestamp ``minutes`` after ``CREATED_AT`` (negative goes back)."""
    return CREATED_AT + timedelta(minutes=minutes)
