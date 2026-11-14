"""Tying a discount line to the item line it reduces (discounts-and-paid-prices plan, section 2).

A discount that names an item's code (``0000399102 / 3139415``, ``TPD/1362394``) is linked to
the nearest preceding ITEM line with that code that has no discount yet. Anything else stays at
receipt level with a reason: exact or nothing, never a guess, and no amount is ever changed.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum, auto

from paper_trail.domain.enums import LineKind
from paper_trail.domain.models import LineItem
from paper_trail.domain.rules.normalization import split_item_code

MULTIPLE = "MULTIPLE"

# "0000399102 / 3139415", "TPD/1362394", "1362394/TPD": anything after the pair is ignored.
_PAIR = re.compile(r"\s*(?P<left>[A-Za-z0-9]+)\s*/\s*(?P<right>[A-Za-z0-9]+)")
_CODE = re.compile(r"[0-9]{4,13}")


class Refusal(StrEnum):
    """Why a discount stays at receipt level: ``MULTIPLE`` (a saving over several items),
    ``NO_REFERENCE`` (no code on the line, "TPD", "INSTANT SAVINGS"), ``NO_ITEM`` (no earlier
    ITEM line with that code that has no discount yet), ``EXCEEDS_ITEM`` (larger than the item's
    line total) or ``NOT_A_SAVING`` (the amount is not negative)."""

    MULTIPLE = auto()
    NO_REFERENCE = auto()
    NO_ITEM = auto()
    EXCEEDS_ITEM = auto()
    NOT_A_SAVING = auto()


@dataclass(frozen=True, slots=True)
class DiscountLink:
    """A discount line tied to its item. ``reference`` is the code as printed on the discount
    line, if any; ``new`` is False for a link the line already carried."""

    discount_position: int
    item_position: int
    reference: str | None
    new: bool


@dataclass(frozen=True, slots=True)
class ReceiptLevelDiscount:
    discount_position: int
    reason: Refusal
    reference: str | None


@dataclass(frozen=True, slots=True)
class DiscountAttribution:
    """Every DISCOUNT line of a receipt, either linked (every effective link, kept and new, by
    discount position) or kept at receipt level."""

    links: tuple[DiscountLink, ...]
    receipt_level: tuple[ReceiptLevelDiscount, ...]

    @property
    def new_links(self) -> tuple[DiscountLink, ...]:
        return tuple(link for link in self.links if link.new)


def discount_reference(raw_text: str) -> str | None:
    """The item code a discount line names, ``MULTIPLE``, or None when it names none.

    In ``A / B`` the code is B when B is 4-13 digits (A is then a coupon number), else A when A
    is and B has no digit at all; a B with digits that is not a code names none. A line whose
    first word is ``MULTIPLE``, or with ``MULTIPLE`` on either side of the pair, in any case, is
    ``MULTIPLE``.
    """
    match = _PAIR.match(raw_text)
    if match is None:
        words = raw_text.split()
        return MULTIPLE if words and words[0].upper() == MULTIPLE else None
    left, right = match["left"], match["right"]
    if MULTIPLE in (left.upper(), right.upper()):
        return MULTIPLE
    if _CODE.fullmatch(right):
        return right
    if _CODE.fullmatch(left) and not any(char.isdigit() for char in right):
        return left
    return None


def attribute_discounts(lines: Sequence[LineItem]) -> DiscountAttribution:
    """Link each unlinked DISCOUNT line to its item, keeping the links already on the lines.

    The links already on the lines are kept and their items count as taken. Every other
    discount, in position order, takes the nearest preceding untaken ITEM line with its code
    (leading zeros ignored), or stays at receipt level: not negative, MULTIPLE, no code, no
    such item, or larger than that item's line total. A refused discount takes no item.

    The pass is repeated on its own output until it adds no link (a discount refused because
    its nearest item was too small finds the next one once a later discount has taken that
    item), so attributing the result again changes nothing: no new link, the same reasons.
    """
    kept = {line.position for line in lines if line.applies_to_position is not None}
    current = tuple(lines)
    found = _attributed(current)
    while found.new_links:
        current = with_attribution(current, found)
        found = _attributed(current)
    return DiscountAttribution(
        tuple(replace(link, new=link.discount_position not in kept) for link in found.links),
        found.receipt_level,
    )


def _attributed(lines: Sequence[LineItem]) -> DiscountAttribution:
    """One pass of the rule over ``lines``."""
    ordered = sorted(lines, key=lambda line: line.position)
    discounts = [line for line in ordered if line.kind is LineKind.DISCOUNT]
    taken = {line.applies_to_position for line in discounts if line.applies_to_position is not None}
    links: list[DiscountLink] = []
    receipt_level: list[ReceiptLevelDiscount] = []
    for discount in discounts:
        reference = discount_reference(discount.raw_text)
        if discount.applies_to_position is not None:
            links.append(
                DiscountLink(discount.position, discount.applies_to_position, reference, new=False)
            )
            continue
        target = _target(ordered, discount, reference, taken)
        if isinstance(target, Refusal):
            receipt_level.append(ReceiptLevelDiscount(discount.position, target, reference))
            continue
        taken.add(target.position)
        links.append(DiscountLink(discount.position, target.position, reference, new=True))
    return DiscountAttribution(tuple(links), tuple(receipt_level))


def with_attribution(lines: Sequence[LineItem], found: DiscountAttribution) -> tuple[LineItem, ...]:
    """``lines`` with ``applies_to_position`` set on the newly linked discounts, all else equal."""
    targets = {link.discount_position: link.item_position for link in found.new_links}
    return tuple(
        replace(line, applies_to_position=targets[line.position])
        if line.position in targets
        else line
        for line in lines
    )


def _target(
    ordered: Sequence[LineItem], discount: LineItem, reference: str | None, taken: set[int]
) -> LineItem | Refusal:
    """The item ``discount`` reduces, else why not (in this order of precedence)."""
    if not discount.line_total.is_negative():
        return Refusal.NOT_A_SAVING
    if reference == MULTIPLE:
        return Refusal.MULTIPLE
    if reference is None:
        return Refusal.NO_REFERENCE
    code = reference.lstrip("0")
    candidates = [
        line
        for line in ordered
        if line.kind is LineKind.ITEM
        and line.position < discount.position
        and line.position not in taken
        and _code(line) == code
    ]
    if not candidates:
        return Refusal.NO_ITEM
    nearest = candidates[-1]
    if -discount.line_total.amount_cents > nearest.line_total.amount_cents:
        return Refusal.EXCEEDS_ITEM
    return nearest


def _code(line: LineItem) -> str | None:
    code, _ = split_item_code(line.raw_text)
    return None if code is None else code.lstrip("0")
