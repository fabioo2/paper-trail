"""Builders for domain objects (PLAN 1.7): plain functions with keyword overrides.

Every builder returns a valid object by default. The defaults describe one consistent receipt
(``receipt_draft()`` passes ``check_arithmetic``), so a test only spells out what it is about.
"""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CorrectionKind,
    FieldSource,
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


def legacy_product(**overrides: Any) -> Product:
    """``product(**overrides)`` as migration 0006 leaves a product stored before codes:
    ``legacy_model`` on every filled field (``size`` when ``size_value`` is set, ``tags`` when
    there are any)."""
    stored = product(**overrides)
    filled = {
        "canonical_name": True,
        "brand": stored.brand is not None,
        "size": stored.size_value is not None,
        "category": stored.category is not None,
        "tags": bool(stored.tags),
    }
    sources = {key: FieldSource.LEGACY_MODEL for key, given in filled.items() if given}
    return replace(stored, field_sources=sources)


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
    values.setdefault("discount", Money.zero(values["unit_price"].currency))
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


def item_line(position: int, raw_text: str, line_total: int, **overrides: Any) -> LineItem:
    """An ITEM line with no printed unit price: 1 x ``line_total`` cents."""
    values: dict[str, Any] = {
        "position": position,
        "raw_text": raw_text,
        "description": raw_text.title(),
        "unit_price": None,
        "line_total": cad(line_total),
    }
    values.update(overrides)
    return line_item(**values)


def discount_line(
    position: int, raw_text: str, line_total: int, applies_to: int | None = None, **overrides: Any
) -> LineItem:
    """A DISCOUNT line of ``line_total`` cents (negative for a saving), linked when
    ``applies_to`` is given."""
    values: dict[str, Any] = {
        "position": position,
        "kind": LineKind.DISCOUNT,
        "raw_text": raw_text,
        "description": "Instant savings",
        "unit_price": None,
        "line_total": cad(line_total),
        "applies_to_position": applies_to,
    }
    values.update(overrides)
    return line_item(**values)


# SIMPLE (tests/data/receipts/costco_simple.json): line 4 TPD/1234567 is the eggs' discount.
SIMPLE_SUBTOTAL, SIMPLE_TAX, SIMPLE_TOTAL = 3240, 52, 3292


def simple_lines(*, link: bool = False) -> tuple[LineItem, ...]:
    """SIMPLE's five lines: eggs 8.99, milk 2 x 3.29, bananas 1.36 kg x 1.50, paper towel 19.79
    and ``TPD/1234567`` -5.00 (linked to line 0 when ``link``)."""
    return (
        line_item(raw_text="1234567 KS ORG EGGS 24CT", category=Category.DAIRY_EGGS),
        line_item(
            position=1,
            raw_text="KS 2% MILK 4L",
            description="Kirkland 2% Milk 4 L",
            quantity=Decimal("2"),
            unit_price=cad(329),
            line_total=cad(658),
            category=Category.DAIRY_EGGS,
        ),
        line_item(
            position=2,
            raw_text="BANANAS 1.36KG",
            description="Bananas",
            quantity=Decimal("1.36"),
            unit_price=cad(150),
            line_total=cad(204),
            category=Category.PRODUCE,
        ),
        line_item(
            position=3,
            raw_text="KS PAPER TOWEL 12RL H",
            description="Kirkland Signature Paper Towels 12 rolls",
            unit_price=cad(1979),
            line_total=cad(1979),
            category=Category.HOUSEHOLD,
        ),
        discount_line(4, "TPD/1234567", -500, 0 if link else None),
    )


def simple_draft(*, link: bool = False, **overrides: Any) -> ReceiptDraft:
    """SIMPLE as a consistent draft: subtotal 32.40, tax 0.52, total 32.92."""
    values: dict[str, Any] = {
        "items": simple_lines(link=link),
        "subtotal": cad(SIMPLE_SUBTOTAL),
        "tax": cad(SIMPLE_TAX),
        "total": cad(SIMPLE_TOTAL),
    }
    values.update(overrides)
    return receipt_draft(**values)


# PRINTED (tests/data/extraction/costco_printed.json as extracted): no printed unit prices.
PRINTED_SUBTOTAL, PRINTED_TAX, PRINTED_TOTAL = 18916, 1403, 20319
PRINTED_LINKS = {6: 5, 8: 7, 10: 9, 12: 11, 14: 13, 16: 15, 18: 17}
PRINTED_MULTIPLE = 4
CHOC = "1362394 CHOC PISTACH"
KIWI = "1543210 GOLD KIWI"


def printed_lines(links: Mapping[int, int] | None = None) -> tuple[LineItem, ...]:
    """PRINTED's 22 lines; ``links`` ({discount position: item position}) sets
    ``applies_to_position`` on those discount lines."""
    links = links or {}
    shape: list[tuple[str, int]] = [
        ("1695232 KIMCHI", 1099),
        ("1556821 PORK RAMEN", 2019),
        ("1234508 DISH PACS", 1899),
        ("1700334 PJ", 2999),
        ("MULTIPLE", -600),
        *[(text, cents) for _ in range(5) for text, cents in ((CHOC, 1699), ("TPD/1362394", -400))],
        *[(text, cents) for _ in range(2) for text, cents in ((KIWI, 1599), ("TPD/1543210", -400))],
        ("1876543 DOUGHNUTS", 1099),
        ("1100231 KS EGGS 24CT", 899),
        ("1281 1% MILK", 609),
    ]
    return tuple(
        discount_line(position, text, cents, links.get(position))
        if cents < 0
        else item_line(position, text, cents)
        for position, (text, cents) in enumerate(shape)
    )


def printed_draft(links: Mapping[int, int] | None = None, **overrides: Any) -> ReceiptDraft:
    """PRINTED as a consistent draft: subtotal 189.16, tax 14.03, total 203.19."""
    values: dict[str, Any] = {
        "store_name": "COSTCO WHOLESALE #9999",
        "purchased_on": date(2026, 11, 5),
        "items": printed_lines(links),
        "subtotal": cad(PRINTED_SUBTOTAL),
        "tax": cad(PRINTED_TAX),
        "total": cad(PRINTED_TOTAL),
        "payment_last4": "0000",
    }
    values.update(overrides)
    return receipt_draft(**values)
