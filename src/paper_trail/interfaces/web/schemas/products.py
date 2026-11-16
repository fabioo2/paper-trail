"""Bodies of the product endpoints (claude-extraction plan D4)."""

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from paper_trail.application.catalog import ProductEdit
from paper_trail.application.enrichment import MAX_NAME_CHARS, MAX_TAG_CHARS
from paper_trail.application.payloads import MAX_TAGS
from paper_trail.domain.enums import Category, FieldSource, Unit
from paper_trail.domain.models import (
    LastPurchase,
    Product,
    ProductSummary,
    is_confirmed,
    require_id,
)
from paper_trail.interfaces.web.schemas.receipts import MoneyOut


class ProductOut(BaseModel):
    """``field_sources`` maps each stamped field (``canonical_name``, ``brand``, ``size``,
    ``category``, ``tags``) to where it came from; ``llm`` is a guess to show as suggested.
    ``confirmed``: a person or a product database named it."""

    id: int
    canonical_name: str
    brand: str | None
    size_value: str | None  # the exact decimal, e.g. "1.36"
    size_unit: Unit | None
    category: Category | None
    tags: list[str]
    field_sources: dict[str, FieldSource]
    confirmed: bool

    @classmethod
    def from_domain(cls, product: Product) -> "ProductOut":
        return cls(
            id=require_id(product.id),
            canonical_name=product.canonical_name,
            brand=product.brand,
            size_value=None if product.size_value is None else str(product.size_value),
            size_unit=product.size_unit,
            category=product.category,
            tags=list(product.tags),
            field_sources=dict(product.field_sources),
            confirmed=is_confirmed(product),
        )


class LastPurchaseOut(BaseModel):
    purchased_on: date | None
    store_name: str
    paid: MoneyOut

    @classmethod
    def from_domain(cls, purchase: LastPurchase) -> "LastPurchaseOut":
        return cls(
            purchased_on=purchase.purchased_on,
            store_name=purchase.store_name,
            paid=MoneyOut.from_domain(purchase.paid),
        )


class ProductSummaryOut(ProductOut):
    """A product as listed: ``times_bought`` counts its receipt lines, ``last_purchase`` is the
    newest of them."""

    times_bought: int
    last_purchase: LastPurchaseOut

    @classmethod
    def from_summary(cls, summary: ProductSummary) -> "ProductSummaryOut":
        return cls(
            **ProductOut.from_domain(summary.product).model_dump(),
            times_bought=summary.times_bought,
            last_purchase=LastPurchaseOut.from_domain(summary.last_purchase),
        )


class ProductUpdateIn(BaseModel):
    """The fields a person changed; a missing or null field is left as it is. ``size`` is text
    like ``500 g``; an empty ``brand`` or ``size``, or no ``tags``, clears the field."""

    model_config = ConfigDict(extra="forbid")

    canonical_name: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    brand: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    size: str | None = Field(default=None, max_length=MAX_NAME_CHARS)
    category: Category | None = None
    tags: list[Annotated[str, Field(max_length=MAX_TAG_CHARS)]] | None = Field(
        default=None, max_length=MAX_TAGS
    )

    def to_edit(self) -> ProductEdit:
        return ProductEdit(
            name=self.canonical_name,
            brand=self.brand,
            size=self.size,
            category=self.category,
            tags=None if self.tags is None else tuple(self.tags),
        )
