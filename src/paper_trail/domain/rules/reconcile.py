"""Reconciling a multi-buy the extraction model missed, from the OCR text (ADR-0017).

A mixed multi-buy prints one amount for several different products ("2 @ 2/$10.00  10.00"
under two products). A model that gives each product the offer's single price ("$5.99 ea or
2/$10.00") leaves the items above the subtotal by N x price - group price. Tesseract usually
reads the multi-buy line, so the gap can be closed from it: exact or nothing, never a guess.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from paper_trail.domain.enums import LineKind
from paper_trail.domain.rules.printed import LineAmount, parse_amount, share_amounts

# "N @ N/$X   X": N units of a group for X, and the amount printed for the group. Tolerates
# Tesseract's misreads ("$" as "S", spaces around "/" and "$"); the count starts at a digit
# boundary, so "12 @ 2/..." is not a "2 @ 2/..." line.
_MULTI_BUY = re.compile(
    r"(?<!\d)(?P<count>\d+)\s*@\s*(?P=count)\s*/\s*[$S]?\s*(?P<price>\d+[.,]\d{2})"
    r"\s+(?P<amount>\d+[.,]\d{2})"
)

_SMALLEST_GROUP = 2


@dataclass(frozen=True, slots=True)
class MultiBuy:
    """The fix: the lines ``first .. first+len(shares)-1`` take ``shares`` as their totals."""

    detail: str  # the multi-buy as printed, e.g. "2 @ 2/$10.00"
    first: int
    shares: tuple[int, ...]


def reconcile_multi_buy(
    lines: Sequence[tuple[LineKind, int]], subtotal_cents: int | None, ocr_text: str
) -> MultiBuy | None:
    """The one multi-buy fix that makes the lines add up to the subtotal exactly, else None.

    A fix needs an OCR line "N @ N/$X  X" (N >= 2, the group price equal to the amount) and a
    run of N adjacent ITEM lines with equal totals p such that items - subtotal = N*p - X. More
    than one candidate, or none, changes nothing.
    """
    if subtotal_cents is None:
        return None
    excess = sum(cents for _, cents in lines) - subtotal_cents
    candidates = {
        fix
        for count, price, detail in _ocr_multi_buys(ocr_text)
        for fix in _runs(lines, count, price, detail)
        if count * lines[fix.first][1] - price == excess
    }
    if len(candidates) != 1 or excess == 0:
        return None
    return candidates.pop()


def _ocr_multi_buys(ocr_text: str) -> list[tuple[int, int, str]]:
    found = []
    for match in _MULTI_BUY.finditer(ocr_text):
        count = int(match["count"])
        price, amount = parse_amount(match["price"]), parse_amount(match["amount"])
        if count >= _SMALLEST_GROUP and price == amount:
            found.append((count, price, f"{count} @ {count}/${match['price']}"))
    return found


def _runs(
    lines: Sequence[tuple[LineKind, int]], count: int, price: int, detail: str
) -> list[MultiBuy]:
    """Every run of ``count`` adjacent ITEM lines with equal totals, as the fix it would be."""
    shares = _shares(count, price)
    return [
        MultiBuy(detail=detail, first=first, shares=shares)
        for first in range(len(lines) - count + 1)
        if all(kind is LineKind.ITEM for kind, _ in lines[first : first + count])
        and len({cents for _, cents in lines[first : first + count]}) == 1
    ]


def _shares(count: int, price: int) -> tuple[int, ...]:
    group = [LineAmount(Decimal(1), None, None)] * (count - 1)
    group.append(LineAmount(Decimal(count), None, price))
    return tuple(line.cents for line in share_amounts(group))
