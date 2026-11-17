"""``ProductRepository`` on SQLite: ``products`` and their ``product_aliases``."""

from dataclasses import replace

from sqlalchemy import Connection, RowMapping, func, insert, select, update

from paper_trail.adapters.sqlite.mappers import (
    alias_from_row,
    alias_values,
    product_from_row,
    product_values,
)
from paper_trail.adapters.sqlite.schema import (
    line_items,
    price_observations,
    product_aliases,
    products,
    receipts,
)
from paper_trail.domain.enums import AliasSource, LineKind
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import (
    LastPurchase,
    Product,
    ProductAlias,
    ProductPurchase,
    ProductSummary,
    require_id,
)
from paper_trail.domain.money import Money


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

    def list(self) -> tuple[Product, ...]:
        rows = self._connection.execute(select(products).order_by(products.c.id)).mappings()
        return tuple(product_from_row(row) for row in rows)

    def summaries(self) -> tuple[ProductSummary, ...]:
        lines = (
            select(
                line_items.c.product_id,
                receipts.c.purchased_on,
                receipts.c.store_name,
                receipts.c.currency,
                func.coalesce(price_observations.c.paid_cents, line_items.c.line_total_cents).label(
                    "paid_cents"
                ),
                func.count().over(partition_by=line_items.c.product_id).label("times_bought"),
                func.row_number()
                .over(partition_by=line_items.c.product_id, order_by=_NEWEST_FIRST)
                .label("newest"),
            )
            .join(receipts, receipts.c.id == line_items.c.receipt_id)
            .outerjoin(price_observations, price_observations.c.line_item_id == line_items.c.id)
            .where(line_items.c.kind == LineKind.ITEM, line_items.c.product_id.is_not(None))
            .subquery()
        )
        rows = self._connection.execute(
            select(
                products,
                lines.c.purchased_on,
                lines.c.store_name,
                lines.c.currency,
                lines.c.paid_cents,
                lines.c.times_bought,
            )
            .join(lines, lines.c.product_id == products.c.id)
            .where(lines.c.newest == 1)
            .order_by(func.lower(products.c.canonical_name), products.c.id)
        ).mappings()
        return tuple(
            ProductSummary(
                product=product_from_row(row),
                times_bought=row["times_bought"],
                last_purchase=LastPurchase(
                    purchased_on=row["purchased_on"],
                    store_name=row["store_name"],
                    paid=Money(row["paid_cents"], row["currency"]),
                ),
            )
            for row in rows
        )

    def purchases(self, product_id: int) -> tuple[ProductPurchase, ...]:
        rows = self._connection.execute(
            select(
                receipts.c.id.label("receipt_id"),
                receipts.c.purchased_on,
                receipts.c.store_name,
                receipts.c.currency,
                line_items.c.quantity,
                line_items.c.line_total_cents,
                func.coalesce(
                    price_observations.c.unit_price_cents, line_items.c.unit_price_cents
                ).label("unit_price_cents"),
                func.coalesce(price_observations.c.discount_cents, 0).label("discount_cents"),
                func.coalesce(price_observations.c.paid_cents, line_items.c.line_total_cents).label(
                    "paid_cents"
                ),
            )
            .join(receipts, receipts.c.id == line_items.c.receipt_id)
            .outerjoin(price_observations, price_observations.c.line_item_id == line_items.c.id)
            .where(line_items.c.kind == LineKind.ITEM, line_items.c.product_id == product_id)
            .order_by(*_NEWEST_FIRST)
        ).mappings()
        return tuple(_purchase_from_row(row) for row in rows)

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

    def alias_at(self, alias: str, store_key: str | None) -> ProductAlias | None:
        row = self._alias_row(alias, store_key)
        return None if row is None else alias_from_row(row)

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


_NEWEST_FIRST = (
    receipts.c.purchased_on.is_(None),
    receipts.c.purchased_on.desc(),
    receipts.c.id.desc(),
    line_items.c.position.desc(),
)


def _purchase_from_row(row: RowMapping) -> ProductPurchase:
    currency: str = row["currency"]
    line_total = Money(row["line_total_cents"], currency)
    return ProductPurchase(
        receipt_id=row["receipt_id"],
        purchased_on=row["purchased_on"],
        store_name=row["store_name"],
        quantity=row["quantity"],
        unit_price=line_total.divided_by(row["quantity"])
        if row["unit_price_cents"] is None
        else Money(row["unit_price_cents"], currency),
        discount=Money(row["discount_cents"], currency),
        paid=Money(row["paid_cents"], currency),
    )


def _outranks(existing: AliasSource, incoming: AliasSource) -> bool:
    """A user's correction is never overwritten by a machine-made alias."""
    return existing is AliasSource.USER_CORRECTION and incoming is not AliasSource.USER_CORRECTION
