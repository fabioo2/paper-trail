"""``ProductRepository`` on SQLite: ``products`` and their ``product_aliases``."""

from dataclasses import replace

from sqlalchemy import Connection, RowMapping, insert, select, update

from paper_trail.adapters.sqlite.mappers import (
    alias_from_row,
    alias_values,
    product_from_row,
    product_values,
)
from paper_trail.adapters.sqlite.schema import product_aliases, products
from paper_trail.domain.enums import AliasSource
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import Product, ProductAlias, require_id


class SqliteProductRepository:
    """Implements ``paper_trail.ports.repositories.ProductRepository``."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def add(self, product: Product) -> Product:
        product_id = self._connection.execute(
            insert(products).values(product_values(product)).returning(products.c.id)
        ).scalar_one()
        return replace(product, id=product_id)

    def get(self, product_id: int) -> Product | None:
        row = (
            self._connection.execute(select(products).where(products.c.id == product_id))
            .mappings()
            .one_or_none()
        )
        return None if row is None else product_from_row(row)

    def update(self, product: Product) -> Product:
        """The ``products_fts`` triggers follow the new name, brand and tags."""
        product_id = require_id(product.id)
        values = {
            key: value for key, value in product_values(product).items() if key != "created_at"
        }
        row = (
            self._connection.execute(
                update(products)
                .where(products.c.id == product_id)
                .values(values)
                .returning(products)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            msg = f"product {product_id} does not exist"
            raise NotFoundError(msg)
        return product_from_row(row)

    def find_by_alias(self, alias: str, store_key: str | None) -> Product | None:
        stores = [None] if store_key is None else [store_key, None]
        for key in stores:
            match = self._alias_row(alias, key)
            if match is not None:
                return self.get(match["product_id"])
        return None

    def add_alias(self, alias: ProductAlias) -> ProductAlias:
        existing = self._alias_row(alias.alias, alias.store_key)
        if existing is None:
            alias_id = self._connection.execute(
                insert(product_aliases).values(alias_values(alias)).returning(product_aliases.c.id)
            ).scalar_one()
            return replace(alias, id=alias_id)
        kept = alias_from_row(existing)
        if _outranks(kept.source, alias.source):
            return kept
        self._connection.execute(
            update(product_aliases)
            .where(product_aliases.c.id == kept.id)
            .values(alias_values(alias))
        )
        return replace(alias, id=kept.id)

    def aliases(self, product_id: int) -> tuple[ProductAlias, ...]:
        rows = self._connection.execute(
            select(product_aliases)
            .where(product_aliases.c.product_id == product_id)
            .order_by(product_aliases.c.id)
        ).mappings()
        return tuple(alias_from_row(row) for row in rows)

    def _alias_row(self, alias: str, store_key: str | None) -> RowMapping | None:
        """The row stored under exactly (alias, store_key); a NULL store key is a key too."""
        return (
            self._connection.execute(
                select(product_aliases).where(
                    product_aliases.c.alias == alias,
                    product_aliases.c.store_key.is_not_distinct_from(store_key),
                )
            )
            .mappings()
            .one_or_none()
        )


def _outranks(existing: AliasSource, incoming: AliasSource) -> bool:
    """A user's correction is never overwritten by a machine-made alias."""
    return existing is AliasSource.USER_CORRECTION and incoming is not AliasSource.USER_CORRECTION
