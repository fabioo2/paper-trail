"""Several catalogs asked in order: the first hit wins (the store catalog, then the PLU table)."""

from paper_trail.domain.rules.item_codes import ItemCode
from paper_trail.ports.catalog import CatalogProduct, ProductCatalog


class CompositeCatalog:
    """Implements ``paper_trail.ports.catalog.ProductCatalog``; later catalogs are not asked
    once one hits."""

    def __init__(self, *catalogs: ProductCatalog) -> None:
        self._catalogs = catalogs

    def lookup(self, code: ItemCode, store_key: str) -> CatalogProduct | None:
        for catalog in self._catalogs:
            found = catalog.lookup(code, store_key)
            if found is not None:
                return found
        return None
