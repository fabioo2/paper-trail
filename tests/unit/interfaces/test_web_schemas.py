"""Decimals in API bodies are fixed point (hardening review round 2): ``str(Decimal)`` can write
``1E+1``, which clients do not parse as a quantity."""

from datetime import date
from decimal import Decimal

import pytest

from paper_trail.application.catalog import shown_value
from paper_trail.domain.enums import Unit
from paper_trail.domain.models import ProductPurchase
from paper_trail.interfaces.web.schemas.products import ProductOut, ProductPurchaseOut
from paper_trail.interfaces.web.schemas.receipts import LineItemOut
from paper_trail.interfaces.web.schemas.reviews import ReviewLineBody
from tests.factories import cad, line_item, product


@pytest.mark.parametrize(
    ("value", "text"),
    [(Decimal("1E+1"), "10"), (Decimal("4.52E-1"), "0.452"), (Decimal("0.4520"), "0.4520")],
)
def test_every_decimal_in_a_body_is_written_in_fixed_point(value: Decimal, text: str) -> None:
    line = line_item(id=1, quantity=value)
    purchase = ProductPurchase(1, date(2026, 11, 1), "COSTCO", value, cad(1), cad(0), cad(1))

    assert [
        LineItemOut.from_domain(line, None).quantity,
        ReviewLineBody.from_line(line).quantity,
        ProductPurchaseOut.from_domain(purchase).quantity,
        ProductOut.from_domain(product(id=1, size_value=value)).size_value,
    ] == [text] * 4


def test_a_size_compared_with_what_was_shown_is_the_size_the_api_wrote() -> None:
    """``ConfirmProduct`` compares the ``expected`` size a client sends back with
    ``shown_value``: both must write the value in fixed point, or the check would always fail."""
    shown = ProductOut.from_domain(product(id=1, size_value=Decimal("1E+1"), size_unit=Unit.G))

    assert shown_value(product(size_value=Decimal("1E+1"), size_unit=Unit.G), "size") == (
        f"{shown.size_value} g"
    )
