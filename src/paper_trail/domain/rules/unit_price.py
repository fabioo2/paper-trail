"""Price per kilogram, litre or piece, to compare pack sizes across stores and receipts."""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from paper_trail.domain.enums import Unit
from paper_trail.domain.money import Money

type BaseUnit = Literal["kg", "l", "each"]

_POUND_IN_KG = Decimal("0.45359237")  # exact by definition
_OUNCES_PER_POUND = 16

# unit -> (how many base units one of it is, base unit); every factor is exact.
_TO_BASE: Mapping[Unit, tuple[Decimal, BaseUnit]] = {
    Unit.G: (Decimal("0.001"), "kg"),
    Unit.KG: (Decimal(1), "kg"),
    Unit.LB: (_POUND_IN_KG, "kg"),
    Unit.OZ: (_POUND_IN_KG / _OUNCES_PER_POUND, "kg"),
    Unit.ML: (Decimal("0.001"), "l"),
    Unit.L: (Decimal(1), "l"),
    Unit.EACH: (Decimal(1), "each"),
}


@dataclass(frozen=True, slots=True)
class BaseUnitPrice:
    cents_per_base: Decimal
    base_unit: BaseUnit


def unit_price_per_base_unit(
    line_total: Money, quantity: Decimal, size_value: Decimal | None, size_unit: Unit | None
) -> BaseUnitPrice | None:
    """``line_total`` per base unit for ``quantity`` packs of ``size_value`` ``size_unit``.

    E.g. 2 x 500 g for 7.98 is 798 cents/kg. None when the pack size or its unit is unknown,
    or when the amount bought is not positive.
    """
    if size_value is None or size_unit is None:
        return None
    factor, base_unit = _TO_BASE[size_unit]
    amount = quantity * size_value * factor
    if amount <= 0:
        return None
    return BaseUnitPrice(Decimal(line_total.amount_cents) / amount, base_unit)
