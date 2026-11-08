"""Price per base unit for comparing pack sizes (S1-AC-07).

Pinned (docs/tests/stage1.md): the amount bought is ``quantity x size_value`` in ``size_unit``;
lb = 0.45359237 kg and oz = lb / 16 exactly; the result is an exact Decimal (no rounding).
"""

from decimal import Decimal

import pytest

from paper_trail.domain.enums import Unit
from paper_trail.domain.rules.unit_price import BaseUnitPrice, unit_price_per_base_unit
from tests.factories import cad


@pytest.mark.parametrize(
    ("cents", "quantity", "size", "unit", "expected"),
    [
        (399, "1", "500", Unit.G, BaseUnitPrice(Decimal("798"), "kg")),
        (549, "1", "2", Unit.L, BaseUnitPrice(Decimal("274.5"), "l")),
        (600, "1", "12", Unit.EACH, BaseUnitPrice(Decimal("50"), "each")),
        (1099, "1", "1", Unit.KG, BaseUnitPrice(Decimal("1099"), "kg")),
        (250, "1", "500", Unit.ML, BaseUnitPrice(Decimal("500"), "l")),
        (798, "2", "500", Unit.G, BaseUnitPrice(Decimal("798"), "kg")),
        (1200, "3", "4", Unit.EACH, BaseUnitPrice(Decimal("100"), "each")),
    ],
)
def test_s1_ac_07_converts_to_the_base_unit(
    cents: int, quantity: str, size: str, unit: Unit, expected: BaseUnitPrice
) -> None:
    """S1-AC-07: g -> kg, ml -> l, EACH -> each; several packs divide by the total amount."""
    result = unit_price_per_base_unit(cad(cents), Decimal(quantity), Decimal(size), unit)

    assert result == expected


def test_s1_ac_07_pounds_and_ounces_convert_to_kilograms() -> None:
    """S1-AC-07: lb and oz -> kg; 16 oz equals 1 lb."""
    pound = unit_price_per_base_unit(cad(454), Decimal("1"), Decimal("1"), Unit.LB)
    ounces = unit_price_per_base_unit(cad(454), Decimal("1"), Decimal("16"), Unit.OZ)

    assert pound is not None
    assert ounces is not None
    assert pound.base_unit == ounces.base_unit == "kg"
    assert round(pound.cents_per_base, 2) == Decimal("1000.90")
    assert round(ounces.cents_per_base, 8) == round(pound.cents_per_base, 8)


@pytest.mark.parametrize(
    ("size", "unit"),
    [(None, None), (Decimal("500"), None), (None, Unit.G)],
)
def test_s1_ac_07_unknown_size_gives_none(size: Decimal | None, unit: Unit | None) -> None:
    """S1-AC-07: without both a size and its unit there is no unit price."""
    assert unit_price_per_base_unit(cad(399), Decimal("1"), size, unit) is None


def test_s1_ac_07_an_empty_pack_gives_none() -> None:
    """A size of zero buys nothing, so there is no price per unit (and no division by zero)."""
    assert unit_price_per_base_unit(cad(399), Decimal("1"), Decimal("0"), Unit.G) is None


def test_s1_ac_07_result_is_exact_decimal() -> None:
    """S1-AC-07: cents_per_base is a Decimal (never a float)."""
    result = unit_price_per_base_unit(cad(549), Decimal("1"), Decimal("2"), Unit.L)

    assert result is not None
    assert isinstance(result.cents_per_base, Decimal)
