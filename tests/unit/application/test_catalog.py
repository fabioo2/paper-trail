"""Naming a new product from a catalog entry (barcode-lookup plan R8, PC-TC-55).

``entry_product_name(name, brand)`` is ``"<brand> <name>"`` unless the name already holds the
brand (ignoring case). The line's model-written ``description`` never takes part.
"""

import pytest

from paper_trail.application.payloads import entry_product_name

ENTRY_NAMES = [
    ("Oat Bars", "Acme", "Acme Oat Bars"),
    ("Acme Oat Bars", "Acme", "Acme Oat Bars"),
    ("Acme oat bars", "ACME", "Acme oat bars"),
    ("Test Apple", None, "Test Apple"),
]


@pytest.mark.parametrize(("name", "brand", "expected"), ENTRY_NAMES)
def test_pc_tc_55_a_new_products_name_from_an_entry(
    name: str, brand: str | None, expected: str
) -> None:
    assert entry_product_name(name, brand) == expected
