"""Products, their aliases and their prices: the writes shared by every use case that links a
receipt line to a product (import, matching, a person's correction), and reading one back."""

from datetime import date, datetime

from paper_trail.domain.enums import AliasSource, Category
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import (
    LineItem,
    PriceObservation,
    Product,
    ProductAlias,
    Receipt,
    require_id,
)
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory


def new_product(uow: UnitOfWork, name: str, category: Category | None, now: datetime) -> Product:
    """Store a product known only by its name (card numbers masked, like all free text)."""
    return uow.products.add(
        Product(
            id=None,
            canonical_name=mask_pans(name).text,
            brand=None,
            size_value=None,
            size_unit=None,
            category=category,
            tags=(),
            created_at=now,
        )
    )


def remember_alias(  # noqa: PLR0913 - the fields of one alias, plus where and when
    uow: UnitOfWork,
    product_id: int,
    alias: str,
    *,
    store_key: str | None,
    source: AliasSource,
    now: datetime,
) -> None:
    """Upsert the alias; a person's (USER_CORRECTION) alias is never replaced by a machine's."""
    uow.products.add_alias(
        ProductAlias(
            id=None,
            product_id=product_id,
            alias=alias,
            store_key=store_key,
            source=source,
            created_at=now,
        )
    )


def price_observation(receipt: Receipt, item: LineItem, purchased_on: date) -> PriceObservation:
    """What the line's product cost on this receipt (the line must be linked)."""
    return PriceObservation(
        id=None,
        product_id=require_id(item.product_id),
        receipt_id=require_id(receipt.id),
        line_item_id=require_id(item.id),
        store_key=receipt.store_key,
        observed_on=purchased_on,
        quantity=item.quantity,
        unit_price=item.line_total.divided_by(item.quantity)
        if item.unit_price is None
        else item.unit_price,
        line_total=item.line_total,
    )


class GetProduct:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self, product_id: int) -> Product:
        with self._uow_factory(read_only=True) as uow:
            product = uow.products.get(product_id)
        if product is None:
            msg = f"product {product_id} does not exist"
            raise NotFoundError(msg)
        return product
