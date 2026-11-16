"""Response body of the product endpoint (claude-extraction plan D4)."""

from pydantic import BaseModel

from paper_trail.domain.enums import Category, FieldSource, Unit
from paper_trail.domain.models import Product, is_confirmed, require_id


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
