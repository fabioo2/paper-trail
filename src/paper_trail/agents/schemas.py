"""What Claude must answer when it reads a receipt photo (claude-extraction plan E1, ADR-0017).

``ReceiptAnswer`` is the ``ReceiptReader``'s schema: the receipt, its finished items and its
adjustments, every amount a string as printed. ``to_reading`` converts it in code into a
``ReceiptReading`` in cents, which the validator checks and the mapper stores.
"""

from collections.abc import Callable
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from paper_trail.application.payloads import CurrencyCode, Last4
from paper_trail.domain.enums import Unit
from paper_trail.domain.errors import PrintedValueError
from paper_trail.domain.money import Money
from paper_trail.domain.rules.printed import (
    TaxReading,
    parse_amount,
    parse_quantity,
    parse_tax,
    parse_unit_price,
    parse_unit_price_exact,
)
from paper_trail.domain.rules.readings import (
    AdjustmentKind,
    ReadAdjustment,
    ReadItem,
    ReceiptReading,
)


class ItemAnswer(BaseModel):
    """One purchased item, finished: its printed line, its discount and what was paid
    (claude-extraction plan E1). Money as printed; a discount as its size."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    printed_text: StrictStr = Field(description="the item's line exactly as printed")
    code: StrictStr | None = Field(description="the product code printed on the line, or null")
    name: StrictStr = Field(description="a readable description of the line")
    department: StrictStr | None = Field(
        description="the department heading printed above the item, or null"
    )
    quantity: StrictStr | None = Field(description='e.g. "2" or "0.365"; null when not printed')
    unit: Literal["each", "kg", "lb"]
    unit_price: StrictStr | None = Field(description="the unit price as printed, or null")
    regular_price: StrictStr = Field(
        description="the line amount before any discount; equals price_paid without one"
    )
    discount: StrictStr | None = Field(description="the saving on this item as printed, or null")
    price_paid: StrictStr = Field(description="the line amount paid, as printed")

    def to_item(self, label: str, problems: list[str], currency: str) -> ReadItem:
        """The item in cents; an unreadable value is added to ``problems`` as
        ``"<label> <field> <why>"`` and zero (one for the quantity) stands in."""

        def money(field: str, text: str) -> Money:
            cents = _read(problems, f"{label} {field}", parse_amount, text, fallback=0)
            return Money(cents, currency)

        unit_price = parse_unit_price(self.unit_price)
        exact = parse_unit_price_exact(self.unit_price)
        printed = None if self.unit_price is None else self.unit_price.strip()
        discount = (
            Money.zero(currency) if self.discount is None else money("discount", self.discount)
        )
        return ReadItem(
            printed_text=self.printed_text,
            code=self.code,
            name=self.name,
            department=self.department,
            quantity=_read(
                problems, f"{label} quantity", parse_quantity, self.quantity, fallback=Decimal(1)
            ),
            unit=Unit(self.unit),
            unit_price=None if unit_price is None else Money(abs(unit_price), currency),
            regular_price=money("regular_price", self.regular_price),
            discount=Money(abs(discount.amount_cents), currency),
            price_paid=money("price_paid", self.price_paid),
            unit_price_exact=None if exact is None else abs(exact),
            unreadable_unit_price=printed if printed and exact is None else None,
        )


class AdjustmentAnswer(BaseModel):
    """A receipt line that is no item: a deposit, a fee or a discount on the whole order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["deposit", "fee", "order_discount"]
    printed_text: StrictStr
    amount: StrictStr = Field(description="as printed")

    def to_adjustment(self, label: str, problems: list[str], currency: str) -> ReadAdjustment:
        """The adjustment as it counts in the receipt sum: an order discount is negative
        whatever its printed sign, a deposit or fee as printed."""
        kind = AdjustmentKind(self.kind)
        cents = _read(problems, f"{label} amount", parse_amount, self.amount, fallback=0)
        if kind is AdjustmentKind.ORDER_DISCOUNT:
            cents = -abs(cents)
        return ReadAdjustment(
            kind=kind, printed_text=self.printed_text, amount=Money(cents, currency)
        )


class ReceiptAnswer(BaseModel):
    """Claude's reading of a receipt photo (E1): the ``ReceiptReader``'s schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    store_name: StrictStr
    purchased_on: StrictStr | None = Field(description="YYYY-MM-DD, or null when not printed")
    currency: CurrencyCode
    subtotal: StrictStr | None
    taxes: list[StrictStr] = Field(description="every tax amount as printed")
    total: StrictStr
    payment_last4: Last4 | None
    items: list[ItemAnswer]
    adjustments: list[AdjustmentAnswer]

    def to_reading(self) -> ReceiptReading:
        """The answer in cents. Every unreadable value is named in one ``PrintedValueError``
        that quotes the printed text and names the item and field (the next attempt's
        feedback). A null quantity is 1, a null discount zero; discount and order discount
        signs as printed do not matter (code applies the minus)."""
        problems: list[str] = []
        currency = self.currency

        def money(field: str, text: str) -> Money:
            return Money(_read(problems, field, parse_amount, text, fallback=0), currency)

        total = money("total", self.total)
        subtotal = None if self.subtotal is None else money("subtotal", self.subtotal)
        taxes = tuple(
            Money(_read(problems, "tax", parse_tax, tax, fallback=TaxReading(0)).cents, currency)
            for tax in self.taxes
        )
        items = tuple(
            item.to_item(f"item {number}", problems, currency)
            for number, item in enumerate(self.items, start=1)
        )
        adjustments = tuple(
            adjustment.to_adjustment(f"adjustment {number}", problems, currency)
            for number, adjustment in enumerate(self.adjustments, start=1)
        )
        if problems:
            raise PrintedValueError("; ".join(problems))
        return ReceiptReading(
            store_name=self.store_name,
            purchased_on=self.purchased_on,
            currency=currency,
            subtotal=subtotal,
            taxes=taxes,
            total=total,
            payment_last4=self.payment_last4,
            items=items,
            adjustments=adjustments,
        )


def _read[T, V](
    problems: list[str], field: str, parse: Callable[[V], T], text: V, *, fallback: T
) -> T:
    """``parse(text)``; when it cannot be read, the problem is noted against ``field`` and
    ``fallback`` stands in, so that every unreadable value is reported at once."""
    try:
        return parse(text)
    except PrintedValueError as error:
        problems.append(f"{field} {error}")
        return fallback
