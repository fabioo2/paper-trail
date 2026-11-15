"""The store catalog: codes that are already ours (``product_codes``), as a ``ProductCatalog``.

It reads through the unit of work, so it serves the fake unit of work as well as SQLite's.
"""

from paper_trail.domain.rules.item_codes import ItemCode
from paper_trail.ports.catalog import CatalogProduct
from paper_trail.ports.unit_of_work import UnitOfWorkFactory


class StoreCatalog:
    """Implements ``paper_trail.ports.catalog.ProductCatalog``: tries the code's identities in
    order (``ItemCode.identities``, at ``ItemCode.identity_at``'s store); a hit is the stored
    product (source ``store_catalog``, ``key`` the identity that hit, ``product_id`` set, the
    other fields the product's)."""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def lookup(self, code: ItemCode, store_key: str) -> CatalogProduct | None:
        at = code.identity_at(store_key)
        if at is None:
            return None
        with self._uow_factory(read_only=True) as uow:
            for identity in code.identities:
                found = uow.product_codes.find(at.scheme, at.store_key, identity)
                product = None if found is None else uow.products.get(found.product_id)
                if product is not None:
                    return CatalogProduct(
                        source="store_catalog",
                        key=identity,
                        product_id=product.id,
                        name=product.canonical_name,
                        brand=product.brand,
                        size_value=product.size_value,
                        size_unit=product.size_unit,
                        category=product.category,
                        tags=product.tags,
                        field_sources=dict(product.field_sources),
                    )
        return None
