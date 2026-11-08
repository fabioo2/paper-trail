"""Does a receipt add up? Code-checked totals, never trusted to a model (PLAN 3.3).

Issue messages are specific ("line 3: 2 x 4.99 = 9.98 but line total is 9.89") because Stage 3
feeds them back to the extraction step verbatim. In every issue ``expected`` is the amount
computed from the other fields and ``actual`` the amount printed on the receipt.
"""

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Protocol

from paper_trail.domain.enums import LineKind
from paper_trail.domain.models import LineItem
from paper_trail.domain.money import Money, sum_money

# Line kinds whose total is a charge and so can never be negative.
_CHARGES = frozenset({LineKind.ITEM, LineKind.DEPOSIT, LineKind.FEE})


class IssueCode(StrEnum):
    LINE_MATH = auto()
    ITEMS_VS_SUBTOTAL = auto()
    SUBTOTAL_PLUS_TAX_VS_TOTAL = auto()
    ITEMS_PLUS_TAX_VS_TOTAL = auto()
    NEGATIVE_ITEM = auto()
    CURRENCY_MISMATCH = auto()
    EMPTY_RECEIPT = auto()
    # Completeness (``check_completeness``): what a receipt needs besides adding up.
    MISSING_STORE = auto()
    MISSING_DATE = auto()


@dataclass(frozen=True, slots=True)
class ArithmeticIssue:
    code: IssueCode
    message: str
    expected: Money | None
    actual: Money | None
    position: int | None  # the line it is about, if any


@dataclass(frozen=True, slots=True)
class Tolerances:
    per_line_cents: int = 1  # quantity x unit price may be printed rounded either way
    total_cents: int = 0


DEFAULT_TOLERANCES = Tolerances()


@dataclass(frozen=True, slots=True)
class ArithmeticReport:
    issues: tuple[ArithmeticIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.issues


class ReceiptLike(Protocol):
    """What the check reads; both ``ReceiptDraft`` and ``Receipt`` are receipt-like."""

    @property
    def items(self) -> Sequence[LineItem]: ...
    @property
    def subtotal(self) -> Money | None: ...
    @property
    def tax(self) -> Money | None: ...
    @property
    def total(self) -> Money | None: ...
    @property
    def currency(self) -> str: ...


def check_arithmetic(
    receipt: ReceiptLike, tolerances: Tolerances = DEFAULT_TOLERANCES
) -> ArithmeticReport:
    """Every arithmetic problem of ``receipt``, found in one pass.

    Rules: (1) quantity x unit price = line total, within ``per_line_cents``; (2) the lines add
    up to the subtotal; (3) subtotal + tax = total; (4) without a subtotal, lines + tax = total;
    (5) only DISCOUNT lines are negative; (6) every amount is in the receipt's currency; (7) a
    receipt has at least one line. Rules 2-4 allow ``total_cents``; a missing tax counts as zero
    and a missing total (still processing) is not checked. When rule 6 fails only its issues
    are reported, because sums across currencies mean nothing.
    """
    mismatches = tuple(_currency_mismatches(receipt))
    if mismatches:
        return ArithmeticReport(mismatches)
    issues = [_empty_receipt()] if not receipt.items else []
    for line in receipt.items:
        issues += _line_issues(line, tolerances)
    issues += _total_issues(receipt, tolerances)
    return ArithmeticReport(tuple(issues))


def _line(position: int) -> str:
    return f"line {position + 1}"


def _differs(expected: Money, actual: Money, tolerance_cents: int) -> bool:
    return abs((expected - actual).amount_cents) > tolerance_cents


def _empty_receipt() -> ArithmeticIssue:
    return ArithmeticIssue(
        IssueCode.EMPTY_RECEIPT, "the receipt has no lines", None, None, position=None
    )


def _currency_mismatches(receipt: ReceiptLike) -> Iterator[ArithmeticIssue]:
    currency = receipt.currency
    amounts: list[tuple[str, Money | None, int | None]] = [
        (f"{_line(line.position)}: {label}", money, line.position)
        for line in receipt.items
        for label, money in (("unit price", line.unit_price), ("line total", line.line_total))
    ]
    amounts += [
        ("subtotal", receipt.subtotal, None),
        ("tax", receipt.tax, None),
        ("total", receipt.total, None),
    ]
    for label, money, position in amounts:
        if money is not None and money.currency != currency:
            message = f"{label} {money.amount_text} {money.currency} is not in {currency}"
            yield ArithmeticIssue(IssueCode.CURRENCY_MISMATCH, message, None, money, position)


def _line_issues(line: LineItem, tolerances: Tolerances) -> list[ArithmeticIssue]:
    issues = []
    if line.kind in _CHARGES and line.line_total.is_negative():
        message = (
            f"{_line(line.position)}: {line.kind} lines cannot be negative,"
            f" but the line total is {line.line_total.amount_text}"
        )
        issues.append(
            ArithmeticIssue(IssueCode.NEGATIVE_ITEM, message, None, line.line_total, line.position)
        )
    if line.unit_price is not None:
        expected = line.unit_price.times(line.quantity)
        if _differs(expected, line.line_total, tolerances.per_line_cents):
            message = (
                f"{_line(line.position)}: {line.quantity} x {line.unit_price.amount_text}"
                f" = {expected.amount_text} but line total is {line.line_total.amount_text}"
            )
            issues.append(
                ArithmeticIssue(
                    IssueCode.LINE_MATH, message, expected, line.line_total, line.position
                )
            )
    return issues


def _total_issues(receipt: ReceiptLike, tolerances: Tolerances) -> list[ArithmeticIssue]:
    tolerance = tolerances.total_cents
    zero = Money.zero(receipt.currency)
    tax = receipt.tax or zero
    items = sum_money((line.line_total for line in receipt.items), receipt.currency)
    issues = []
    if receipt.subtotal is not None and _differs(items, receipt.subtotal, tolerance):
        message = (
            f"items add up to {items.amount_text}"
            f" but the subtotal is {receipt.subtotal.amount_text}"
        )
        issues.append(
            ArithmeticIssue(IssueCode.ITEMS_VS_SUBTOTAL, message, items, receipt.subtotal, None)
        )
    if receipt.total is None:
        return issues
    if receipt.subtotal is not None:
        code, base, label = IssueCode.SUBTOTAL_PLUS_TAX_VS_TOTAL, receipt.subtotal, "subtotal"
    else:
        code, base, label = IssueCode.ITEMS_PLUS_TAX_VS_TOTAL, items, "items"
    expected = base + tax
    if _differs(expected, receipt.total, tolerance):
        message = (
            f"{label} {base.amount_text} + tax {tax.amount_text} = {expected.amount_text}"
            f" but the total is {receipt.total.amount_text}"
        )
        issues.append(ArithmeticIssue(code, message, expected, receipt.total, None))
    return issues
