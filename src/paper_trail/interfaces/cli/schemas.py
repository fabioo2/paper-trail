"""The JSON format of ``paper-trail receipts import`` (PLAN 3.8).

Strict on purpose: unknown fields are errors (a typo must not drop data silently) and cents are
JSON integers, never floats or strings, so no amount is ever rounded on the way in.
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, StrictInt

from paper_trail.application.payloads import CurrencyCode, Last4, Quantity
from paper_trail.domain.enums import Category, LineKind
from paper_trail.domain.models import LineItem, ReceiptDraft
from paper_trail.domain.money import DEFAULT_CURRENCY, Money


class LineImport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_text: str
    description: str | None = None  # defaults to raw_text
    quantity: Quantity = Decimal(1)
    unit_price_cents: StrictInt | None = None
    line_total_cents: StrictInt
    kind: LineKind = LineKind.ITEM
    product: str | None = None  # the product this line is, linked or created on import
    category: Category | None = None

    def to_line_item(self, position: int, currency: str) -> LineItem:
        return LineItem(
            position=position,
            kind=self.kind,
            raw_text=self.raw_text,
            description=self.raw_text if self.description is None else self.description,
            quantity=self.quantity,
            unit_price=None
            if self.unit_price_cents is None
            else Money(self.unit_price_cents, currency),
            line_total=Money(self.line_total_cents, currency),
            category=self.category,
        )


class ReceiptImport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    store_name: str
    purchased_on: date | None = None
    currency: CurrencyCode = DEFAULT_CURRENCY
    subtotal_cents: StrictInt | None = None
    tax_cents: StrictInt | None = None
    total_cents: StrictInt
    payment_last4: Last4 | None = None
    items: list[LineImport]

    def to_draft(self) -> ReceiptDraft:
        """The draft to record; every amount is in the receipt's currency."""

        def money(cents: int | None) -> Money | None:
            return None if cents is None else Money(cents, self.currency)

        return ReceiptDraft(
            store_name=self.store_name,
            purchased_on=self.purchased_on,
            currency=self.currency,
            subtotal=money(self.subtotal_cents),
            tax=money(self.tax_cents),
            total=Money(self.total_cents, self.currency),
            items=tuple(
                line.to_line_item(position, self.currency)
                for position, line in enumerate(self.items)
            ),
            payment_last4=self.payment_last4,
            ocr_text=None,
            image_path=None,
            product_names={
                position: line.product
                for position, line in enumerate(self.items)
                if line.product is not None
            },
        )
