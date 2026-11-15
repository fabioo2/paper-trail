"""Describing products from catalogs, never from a model (barcode-lookup plan 4.2, ADR-0018).

``describe_product`` is what the supervisor's ``describe_from_catalog`` tool writes: only the
fields that come with a source, never over a person's, and a person's category for the line at
that store over any source's. It reads the product again inside its unit of work, so a person's
write between the caller's read and this one is kept. It never embeds.
"""

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

from paper_trail.application.matching import latest_correction
from paper_trail.application.payloads import FieldSourceKey, ProductFieldsPayload
from paper_trail.domain.enums import Category, CorrectionKind, FieldSource
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import Product
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.ports.unit_of_work import UnitOfWorkFactory


def describe_product(  # noqa: PLR0913 - the product, its fields and sources, and the line
    uow_factory: UnitOfWorkFactory,
    product_id: int,
    *,
    fields: ProductFieldsPayload,
    sources: Mapping[FieldSourceKey, FieldSource],
    alias: str,
    store_key: str,
) -> Product:
    """Write ``fields`` with their ``sources`` onto the product, in one unit of work, and return
    it as stored. A field whose stored source is ``person`` is kept (per field). The newest
    CATEGORY correction for ``alias`` (a line's raw text, both normalizations tried) at
    ``store_key`` sets the category with source ``person``, given a category or not. Free text
    is masked. Nothing is written when nothing changes. NotFoundError (nothing written) for an
    unknown product."""
    with uow_factory() as uow:
        product = uow.products.get(product_id)
        if product is None:
            msg = f"product {product_id} does not exist"
            raise NotFoundError(msg)
        correction = latest_correction(uow, CorrectionKind.CATEGORY, alias, store_key)
        remembered = (
            Category(correction.new_value)
            if correction is not None and correction.new_value in Category
            else None
        )
        described = _described(product, fields, sources, remembered)
        if described == product:
            return product
        stored = uow.products.update(described)
        uow.commit()
    return stored


def _described(
    product: Product,
    fields: ProductFieldsPayload,
    sources: Mapping[FieldSourceKey, FieldSource],
    remembered: Category | None,
) -> Product:
    """``product`` with every sourced field written, except where a person's value stands."""
    written = dict(product.field_sources)
    described = product

    def open_for(key: FieldSourceKey) -> bool:
        if key not in sources or written.get(key) is FieldSource.PERSON:
            return False
        written[key] = sources[key]
        return True

    if fields.name is not None and open_for("canonical_name"):
        described = replace(described, canonical_name=mask_pans(fields.name).text)
    if fields.brand is not None and open_for("brand"):
        described = replace(described, brand=mask_pans(fields.brand).text)
    if fields.size_value is not None and open_for("size"):
        described = replace(
            described, size_value=Decimal(fields.size_value), size_unit=fields.size_unit
        )
    if remembered is not None:
        if written.get("category") is not FieldSource.PERSON:
            described = replace(described, category=remembered)
            written["category"] = FieldSource.PERSON
    elif fields.category is not None and open_for("category"):
        described = replace(described, category=fields.category)
    if fields.tags is not None and open_for("tags"):
        described = replace(described, tags=tuple(mask_pans(tag).text for tag in fields.tags))
    return replace(described, field_sources=written)
