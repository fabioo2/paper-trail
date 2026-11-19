"""A receipt as Claude reads it, item by item, in cents: the validator (claude-extraction plan
E2) and the storage mapper (E4).

The agents' answer model converts the printed strings into a ``ReceiptReading``
(``ReceiptAnswer.to_reading``); nothing here parses text. Discounts are sizes (never negative,
zero for none); an order discount's amount is negative, a deposit's or fee's is as printed (a
bottle return may be negative).

``check_reading`` lists every failure at once, as ``ArithmeticIssue`` so that the retry
feedback, review items and the trace read as they do today:

- per item, exactly: regular price - discount = price paid (``ITEM_PRICE_MATH``); a price paid
  below zero is ``NEGATIVE_ITEM``;
- an item with a printed unit price, weighed (unit kg or lb) or bought more than once
  (quantity above 1): quantity x unit price matches the regular or the paid price within
  ``per_line_cents`` (``LINE_MATH``), so a pack price is never read as one unit's price; the
  product is exact, then rounded to the cent (a deal "3/$10.00" is 10.00/3 a unit, so 6 of
  them are 20.00); a unit price printed but unreadable is a ``LINE_MATH`` issue too;
- the receipt: sum of prices paid + adjustments = subtotal (total - taxes without one), and
  subtotal + taxes = total, within ``total_cents``; taxes are ignored when the printed subtotal
  equals the printed total (tax-inclusive receipts, as ``printed_tax`` does today).

Store name and date are checked by ``check_completeness``, as today.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum, auto
from fractions import Fraction

from paper_trail.domain.enums import LineKind, Unit
from paper_trail.domain.models import LineItem, ReceiptDraft
from paper_trail.domain.money import Money, sum_money
from paper_trail.domain.rules.arithmetic import (
    DEFAULT_TOLERANCES,
    ArithmeticIssue,
    ArithmeticReport,
    IssueCode,
    Tolerances,
)
from paper_trail.domain.rules.dates import parse_receipt_date
from paper_trail.domain.rules.departments import department_category
from paper_trail.domain.rules.normalization import normalize_store_name
from paper_trail.domain.rules.printed import printed_tax

WEIGHED_UNITS = frozenset({Unit.KG, Unit.LB})


class AdjustmentKind(StrEnum):
    DEPOSIT = auto()
    FEE = auto()
    ORDER_DISCOUNT = auto()


@dataclass(frozen=True, slots=True)
class ReadItem:
    printed_text: str  # stored as the ITEM line's raw_text
    code: str | None  # the product code printed on the line, as printed
    name: str  # the line's description (never a product name in PR 1)
    department: str | None  # the printed department heading
    quantity: Decimal  # 1 when none is printed
    unit: Unit  # EACH, KG or LB
    unit_price: Money | None
    regular_price: Money  # the line amount before the discount, quantity included
    discount: Money  # its size; zero when none
    price_paid: Money  # the line amount paid, quantity included
    # The printed unit price in cents, exact: a deal "3/$10.00" is 1000/3. None: ``unit_price``
    # is exact.
    unit_price_exact: Fraction | None = None
    # A unit price printed but not readable as one, as printed: a LINE_MATH issue, never a
    # silently skipped check.
    unreadable_unit_price: str | None = None


@dataclass(frozen=True, slots=True)
class ReadAdjustment:
    kind: AdjustmentKind
    printed_text: str
    amount: Money  # as it counts in the receipt sum: an order discount is negative


@dataclass(frozen=True, slots=True)
class ReceiptReading:
    store_name: str
    purchased_on: str | None  # YYYY-MM-DD as answered; parsed in code
    currency: str
    subtotal: Money | None
    taxes: tuple[Money, ...]
    total: Money
    payment_last4: str | None
    items: tuple[ReadItem, ...]
    adjustments: tuple[ReadAdjustment, ...]


def check_reading(
    reading: ReceiptReading, tolerances: Tolerances = DEFAULT_TOLERANCES
) -> ArithmeticReport:
    """Every failure of the E2 rules (module docstring), found in one pass. Messages name the
    item by number (1-based) and printed text, and the amounts compared; an item's issue is at
    the position of its ITEM line in ``reading_draft``."""
    issues: list[ArithmeticIssue] = []
    if not reading.items:
        issues.append(
            ArithmeticIssue(IssueCode.EMPTY_RECEIPT, "the receipt has no items", None, None, None)
        )
    for number, (position, item) in enumerate(_item_positions(reading), start=1):
        issues += _item_issues(number, position, item, tolerances)
    issues += _receipt_issues(reading, tolerances)
    return ArithmeticReport(tuple(issues))


def reading_draft(
    reading: ReceiptReading, *, today: date | None, image_path: str | None
) -> ReceiptDraft:
    """The draft to save (E4): per item an ITEM line (line total = regular price, its name as
    the description, its department's category hint) followed, when its discount is above
    zero, by a DISCOUNT line of -discount linked to it; then one line per adjustment: DEPOSIT,
    FEE, or an unlinked DISCOUNT for an order discount. Every line's raw text is its printed
    text. The date is parsed against ``today`` as ``ExtractedReceipt.to_draft`` does; the tax
    is ``printed_tax`` of the subtotal, total and taxes. ``ocr_text`` is None.

    The ITEM line's unit price is its regular price divided by its quantity, so that the stored
    line multiplies back to its total; None when no such price does (within one cent). An item
    whose printed unit price fails ``LINE_MATH`` keeps that printed price instead, so the draft
    fails the same check and a person sees (and must fix or force) the conflict. The
    mapping never fails: a discount larger than its item's regular price (which
    ``check_reading`` reports) stays unlinked, at receipt level."""
    store_key = normalize_store_name(reading.store_name)
    lines: list[LineItem] = []
    for position, item in _item_positions(reading):
        lines.append(
            LineItem(
                position=position,
                kind=LineKind.ITEM,
                raw_text=item.printed_text,
                description=item.name,
                quantity=item.quantity,
                unit_price=_unit_price(item),
                line_total=item.regular_price,
                category=department_category(store_key, item.department),
                unit=item.unit,
            )
        )
        if item.discount.amount_cents > 0:
            lines.append(
                LineItem(
                    position=position + 1,
                    kind=LineKind.DISCOUNT,
                    raw_text=item.printed_text,
                    description=item.name,
                    quantity=Decimal(1),
                    unit_price=None,
                    line_total=-item.discount,
                    applies_to_position=position if _linkable(item) else None,
                )
            )
    for adjustment in reading.adjustments:
        lines.append(
            LineItem(
                position=len(lines),
                kind=_ADJUSTMENT_KINDS[adjustment.kind],
                raw_text=adjustment.printed_text,
                description=adjustment.printed_text,
                quantity=Decimal(1),
                unit_price=None,
                line_total=adjustment.amount,
            )
        )
    currency = reading.currency
    subtotal = None if reading.subtotal is None else reading.subtotal.amount_cents
    tax = printed_tax(
        subtotal, reading.total.amount_cents, [tax.amount_cents for tax in reading.taxes]
    )
    return ReceiptDraft(
        store_name=reading.store_name,
        purchased_on=None
        if today is None or reading.purchased_on is None
        else parse_receipt_date(reading.purchased_on, today),
        currency=currency,
        subtotal=reading.subtotal,
        tax=None if tax is None else Money(tax, currency),
        total=reading.total,
        items=tuple(lines),
        payment_last4=reading.payment_last4,
        ocr_text=None,
        image_path=image_path,
    )


def reading_codes(reading: ReceiptReading) -> Mapping[int, str]:
    """The printed product code of each item that has one, by the position of its ITEM line in
    ``reading_draft``: what matching uses instead of parsing codes out of the raw text."""
    return {
        position: item.code.strip()
        for position, item in _item_positions(reading)
        if item.code is not None and item.code.strip()
    }


def reading_departments(reading: ReceiptReading) -> Mapping[int, str]:
    """The printed department heading of each item that has one, by the position of its ITEM
    line in ``reading_draft``: what enrichment tells the guess."""
    return {
        position: item.department.strip()
        for position, item in _item_positions(reading)
        if item.department is not None and item.department.strip()
    }


_ADJUSTMENT_KINDS = {
    AdjustmentKind.DEPOSIT: LineKind.DEPOSIT,
    AdjustmentKind.FEE: LineKind.FEE,
    AdjustmentKind.ORDER_DISCOUNT: LineKind.DISCOUNT,
}


def _item_positions(reading: ReceiptReading) -> Iterator[tuple[int, ReadItem]]:
    """Each item with the position of its ITEM line: one line per item, two with a discount."""
    position = 0
    for item in reading.items:
        yield position, item
        position += 2 if item.discount.amount_cents > 0 else 1


def _linkable(item: ReadItem) -> bool:
    """A discount links to its item only when the item can carry it (a stored discount is never
    larger than its item's line total)."""
    return item.regular_price.amount_cents >= item.discount.amount_cents


def _unit_price(item: ReadItem) -> Money | None:
    if item.unit_price is not None and _weighed_mismatch(item, DEFAULT_TOLERANCES) is not None:
        return item.unit_price
    price = item.regular_price.divided_by(item.quantity)
    off = abs((price.times(item.quantity) - item.regular_price).amount_cents)
    return price if off <= DEFAULT_TOLERANCES.per_line_cents else None


def _named(number: int, item: ReadItem) -> str:
    return f'item {number} "{item.printed_text}"'


def _item_issues(
    number: int, position: int, item: ReadItem, tolerances: Tolerances
) -> list[ArithmeticIssue]:
    issues: list[ArithmeticIssue] = []
    regular, discount, paid = item.regular_price, item.discount, item.price_paid
    if paid.is_negative():
        message = (
            f"{_named(number, item)}: price paid {paid.amount_text} cannot be negative;"
            " only an order discount reduces the receipt"
        )
        issues.append(ArithmeticIssue(IssueCode.NEGATIVE_ITEM, message, None, paid, position))
    expected = regular - discount
    if expected != paid:
        message = (
            f"{_named(number, item)}: regular {regular.amount_text} \N{MINUS SIGN} discount"
            f" {discount.amount_text} \N{NOT EQUAL TO} paid {paid.amount_text}"
        )
        issues.append(ArithmeticIssue(IssueCode.ITEM_PRICE_MATH, message, expected, paid, position))
    if item.unreadable_unit_price is not None:
        message = (
            f'{_named(number, item)}: the unit price "{item.unreadable_unit_price}" cannot be'
            ' read: copy it as printed, e.g. "2.18", "$2.18/kg" or "3/$10.00"'
        )
        issues.append(ArithmeticIssue(IssueCode.LINE_MATH, message, None, regular, position))
    weighed = _weighed_mismatch(item, tolerances)
    if item.unit_price is not None and weighed is not None:
        message = (
            f"{_named(number, item)}: {item.quantity} {item.unit.value} x"
            f" {item.unit_price.amount_text} = {weighed.amount_text}, but the regular price"
            f" is {regular.amount_text} and the price paid {paid.amount_text}"
        )
        issues.append(ArithmeticIssue(IssueCode.LINE_MATH, message, weighed, regular, position))
    return issues


def _weighed_mismatch(item: ReadItem, tolerances: Tolerances) -> Money | None:
    """Quantity x the printed unit price, when it is checked (weighed, or bought more than
    once) and matches neither the regular nor the paid price; else None."""
    if item.unit_price is None or not (item.unit in WEIGHED_UNITS or item.quantity > 1):
        return None
    unit = item.unit_price_exact
    exact = (Fraction(item.unit_price.amount_cents) if unit is None else unit) * Fraction(
        item.quantity
    )
    weighed = Money(_rounded(exact), item.unit_price.currency)
    prices = (item.regular_price, item.price_paid)
    if all(_differs(weighed, price, tolerances.per_line_cents) for price in prices):
        return weighed
    return None


def _receipt_issues(reading: ReceiptReading, tolerances: Tolerances) -> list[ArithmeticIssue]:
    currency, tolerance = reading.currency, tolerances.total_cents
    paid = sum_money((item.price_paid for item in reading.items), currency)
    adjusted = paid + sum_money((adjustment.amount for adjustment in reading.adjustments), currency)
    subtotal = reading.subtotal
    tax_inclusive = subtotal is not None and subtotal == reading.total
    tax = Money.zero(currency) if tax_inclusive else sum_money(reading.taxes, currency)
    sums = f"items paid {paid.amount_text} + adjustments {(adjusted - paid).amount_text}"
    issues: list[ArithmeticIssue] = []
    if subtotal is not None:
        if _differs(adjusted, subtotal, tolerance):
            message = (
                f"{sums} = {adjusted.amount_text} but the subtotal is {subtotal.amount_text}"
                f" (difference {_difference(adjusted, subtotal)})"
            )
            issues.append(
                ArithmeticIssue(IssueCode.ITEMS_VS_SUBTOTAL, message, adjusted, subtotal, None)
            )
        expected = subtotal + tax
        if _differs(expected, reading.total, tolerance):
            message = (
                f"subtotal {subtotal.amount_text} + taxes {tax.amount_text}"
                f" = {expected.amount_text} but the total is {reading.total.amount_text}"
                f" (difference {_difference(expected, reading.total)})"
            )
            issues.append(
                ArithmeticIssue(
                    IssueCode.SUBTOTAL_PLUS_TAX_VS_TOTAL, message, expected, reading.total, None
                )
            )
        return issues
    expected = adjusted + tax
    if _differs(expected, reading.total, tolerance):
        message = (
            f"{sums} + taxes {tax.amount_text} = {expected.amount_text}"
            f" but the total is {reading.total.amount_text}"
            f" (difference {_difference(expected, reading.total)})"
        )
        issues.append(
            ArithmeticIssue(
                IssueCode.ITEMS_PLUS_TAX_VS_TOTAL, message, expected, reading.total, None
            )
        )
    return issues


def _rounded(cents: Fraction) -> int:
    """``cents`` to the nearest cent, half away from zero (as ``Money.times``)."""
    return int((Decimal(cents.numerator) / cents.denominator).quantize(Decimal(1), ROUND_HALF_UP))


def _differs(expected: Money, actual: Money, tolerance_cents: int) -> bool:
    return abs((expected - actual).amount_cents) > tolerance_cents


def _difference(expected: Money, actual: Money) -> str:
    return f"{abs((expected - actual).amount_cents)} \N{CENT SIGN}"
