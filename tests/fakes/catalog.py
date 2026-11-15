"""A catalog from a table (implements ``paper_trail.ports.catalog.ProductCatalog``).

``FakeCatalog({"3101": entry})`` answers a lookup with the entry of the first of the code's
lookup keys (``ItemCode.keys``) in the table, else None, at any store. ``calls`` records every
lookup as (code, store_key), so a test can pin that a catalog was not asked.
"""

from collections.abc import Mapping

from paper_trail.domain.enums import Category
from paper_trail.domain.rules.item_codes import ItemCode
from paper_trail.ports.catalog import CatalogProduct


class FakeCatalog:
    def __init__(self, entries: Mapping[str, CatalogProduct] | None = None) -> None:
        self._entries = dict(entries or {})
        self.calls: list[tuple[ItemCode, str]] = []

    def lookup(self, code: ItemCode, store_key: str) -> CatalogProduct | None:
        self.calls.append((code, store_key))
        return next((self._entries[key] for key in code.keys if key in self._entries), None)


TEST_APPLE = CatalogProduct(
    "plu", "3101", None, "Test Apple", None, None, None, Category.PRODUCE, ()
)
ORGANIC_TEST_APPLE = CatalogProduct(
    "plu", "3101", None, "Organic Test Apple", None, None, None, Category.PRODUCE, ("organic",)
)
