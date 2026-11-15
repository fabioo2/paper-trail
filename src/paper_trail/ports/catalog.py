"""Product catalogs: what a code printed on a receipt line names (barcode-lookup plan 3.1).

The store catalog is our own ``product_codes``; reference sources (``plu.csv`` now, Open Food
Facts later) describe products we may not have yet. A missing source is a miss, never an error.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from paper_trail.domain.enums import Category, FieldSource, Unit
from paper_trail.domain.rules.item_codes import ItemCode

type CatalogSource = Literal["store_catalog", "plu"]


@dataclass(frozen=True, slots=True)
class CatalogProduct:
    """One source's entry for a code. ``product_id`` is set when the code is one of our
    products (``store_catalog``); the other fields then describe that product as stored, and
    ``field_sources`` are its own (empty for a reference source: its fields are its own)."""

    source: CatalogSource
    key: str  # the code that hit: an identity for the store catalog, a lookup key otherwise
    product_id: int | None
    name: str | None
    brand: str | None
    size_value: Decimal | None
    size_unit: Unit | None
    category: Category | None
    tags: tuple[str, ...]
    categories: tuple[str, ...] = ()  # a source's own category tags (Open Food Facts, PR 2)
    quantity_text: str | None = None  # a source's size as printed (Open Food Facts, PR 2)
    field_sources: Mapping[str, FieldSource] = field(default_factory=dict)


class ProductCatalog(Protocol):
    def lookup(self, code: ItemCode, store_key: str) -> CatalogProduct | None:
        """The entry for ``code`` read at ``store_key``, or None. Never opens a connection."""
        ...
