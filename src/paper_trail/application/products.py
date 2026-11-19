"""Products, their aliases and their prices: the writes shared by every use case that links a
receipt line to a product (import, matching, a person's correction), reading one back, and
merging two products that are the same one."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime

import structlog

from paper_trail.domain.enums import AliasSource, Category, FieldSource
from paper_trail.domain.errors import ConflictError, DomainValidationError, NotFoundError
from paper_trail.domain.models import (
    CONFIRMING_SOURCES,
    FIELD_SOURCE_KEYS,
    LineItem,
    PriceObservation,
    Product,
    ProductAlias,
    ProductPurchase,
    ProductSummary,
    Receipt,
    require_id,
)
from paper_trail.domain.money import Money
from paper_trail.domain.rules.masking import mask_pans
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger(__name__)

MERGE_CANDIDATES = 10


def new_product(
    uow: UnitOfWork,
    name: str,
    category: Category | None,
    now: datetime,
    *,
    field_sources: Mapping[str, FieldSource],
) -> Product:
    """Store a product known only by its name and maybe its category (card numbers masked,
    like all free text), with where each came from."""
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
            field_sources=dict(field_sources),
        )
    )


def person_named(category: Category | None) -> dict[str, FieldSource]:
    """The field sources of a product a person named (and maybe categorized)."""
    sources = {"canonical_name": FieldSource.PERSON}
    if category is not None:
        sources["category"] = FieldSource.PERSON
    return sources


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
    """What the line's product cost on this receipt (the line must be linked): its regular
    price, and the discount line that applies to it, if any."""
    discount = next(
        (
            line.line_total.amount_cents
            for line in receipt.items
            if line.applies_to_position == item.position
        ),
        0,
    )
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
        discount=Money(discount, item.line_total.currency),
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


class GetProducts:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self, product_ids: Iterable[int]) -> dict[int, Product]:
        """The stored products among ``product_ids``, by id; unknown ids are left out. Read-only."""
        with self._uow_factory(read_only=True) as uow:
            return uow.products.get_many(set(product_ids))


class ListProducts:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self) -> tuple[ProductSummary, ...]:
        """Every product bought at least once, by name, with its newest purchase. Read-only."""
        with self._uow_factory(read_only=True) as uow:
            return uow.products.summaries()


class ListProductPurchases:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self, product_id: int) -> tuple[ProductPurchase, ...]:
        """The product's purchases, newest first; NotFoundError for an unknown id. Read-only."""
        with self._uow_factory(read_only=True) as uow:
            if uow.products.get(product_id) is None:
                msg = f"product {product_id} does not exist"
                raise NotFoundError(msg)
            return uow.products.purchases(product_id)


@dataclass(frozen=True, slots=True)
class ProductMerge:
    """What merging ``merged`` into ``into`` does (``preview``) or did: ``into`` as it was
    before, ``kept`` as it is stored after the merge, and how many purchases (ITEM lines), codes
    and aliases move."""

    merged: Product
    into: Product
    kept: Product
    purchases: int
    codes: int
    aliases: int


class MergeProducts:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def preview(self, product_id: int, into_id: int) -> ProductMerge:
        """What ``__call__`` would do, from a read-only unit; writes nothing. Raises what
        ``__call__`` raises, but for a receipt being read (a preview waits for nothing)."""
        with self._uow_factory(read_only=True) as uow:
            return _merge_plan(uow, product_id, into_id)

    def __call__(self, product_id: int, into_id: int) -> ProductMerge:
        """Merge the product into ``into_id`` in one unit of work: its lines, price
        observations, codes and aliases become the kept product's (each row kept as it is but
        for its product), PRODUCT_LINK corrections naming it name the kept product, each field
        of the kept product takes the merged one's value where that one is better sourced
        (``merged_fields``), and the product is deleted. DomainValidationError for a product
        merged into itself (checked first), NotFoundError naming an unknown id, ConflictError
        (nothing written) while a job waits or runs (the CLI's holds included)."""
        with self._uow_factory() as uow:
            merge = _merge_plan(uow, product_id, into_id)
            if busy := uow.jobs.active_receipts():
                raise ConflictError(busy_message(busy))
            uow.receipts.move_lines(product_id, into_id)
            uow.prices.move_product(product_id, into_id)
            uow.product_codes.move_product(product_id, into_id)
            uow.products.move_aliases(product_id, into_id)
            uow.corrections.rename_product(product_id, into_id)
            kept = uow.products.update(merge.kept)
            uow.products.delete(product_id)
            uow.commit()
        log.info(
            "products.merged",
            product_id=product_id,
            into_id=into_id,
            purchases=merge.purchases,
            codes=merge.codes,
            aliases=merge.aliases,
        )
        return replace(merge, kept=kept)


def _merge_plan(uow: UnitOfWork, product_id: int, into_id: int) -> ProductMerge:
    """The merge, checked and read through ``uow`` (nothing written)."""
    if product_id == into_id:
        msg = f"product {product_id} cannot be merged into itself"
        raise DomainValidationError(msg)
    merged = _existing(uow, product_id)
    kept = _existing(uow, into_id)
    return ProductMerge(
        merged=merged,
        into=kept,
        kept=merged_fields(kept, merged),
        purchases=len(uow.receipts.item_lines(product_id)),
        codes=len(uow.product_codes.for_product(product_id)),
        aliases=len(uow.products.aliases(product_id)),
    )


def busy_message(receipt_ids: Sequence[int]) -> str:
    """Why a merge waits: the receipts being read, by id."""
    if len(receipt_ids) == 1:
        return f"Receipt {receipt_ids[0]} is being read. Try again in a moment."
    named = ", ".join(str(receipt_id) for receipt_id in receipt_ids)
    return f"Receipts {named} are being read. Try again in a moment."


def _existing(uow: UnitOfWork, product_id: int) -> Product:
    product = uow.products.get(product_id)
    if product is None:
        msg = f"product {product_id} does not exist"
        raise NotFoundError(msg)
    return product


def merged_fields(kept: Product, merged: Product) -> Product:
    """``kept`` with each field (the size's value and unit together) taken from ``merged``
    where that one ranks higher (``field_rank``), with its source; ties keep ``kept``'s.
    ``id`` and ``created_at`` are ``kept``'s."""
    product = kept
    sources = dict(kept.field_sources)
    for key in FIELD_SOURCE_KEYS:
        if field_rank(merged, key) <= field_rank(kept, key):
            continue
        match key:
            case "canonical_name":
                product = replace(product, canonical_name=merged.canonical_name)
            case "brand":
                product = replace(product, brand=merged.brand)
            case "size":
                product = replace(product, size_value=merged.size_value, size_unit=merged.size_unit)
            case "category":
                product = replace(product, category=merged.category)
            case "tags":
                product = replace(product, tags=merged.tags)
        if (source := merged.field_sources.get(key)) is None:
            sources.pop(key, None)
        else:
            sources[key] = source
    return replace(product, field_sources=sources)


def field_rank(product: Product, key: str) -> int:
    """How good a field is: a person's 3, a product database's or the PLU table's 2, printed,
    guessed or unsourced 1, empty 0."""
    filled = {
        "canonical_name": True,
        "brand": product.brand is not None,
        "size": product.size_value is not None,
        "category": product.category is not None,
        "tags": bool(product.tags),
    }
    if not filled[key]:
        return 0
    source = product.field_sources.get(key)
    if source is FieldSource.PERSON:
        return 3
    return 2 if source in CONFIRMING_SOURCES else 1


class MergeCandidates:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self, product_id: int) -> tuple[ProductSummary, ...]:
        """The bought products most likely the same as this one, at most
        ``MERGE_CANDIDATES``: those holding one of its codes' digits (leading zeros ignored, any
        scheme or store; a code of zeros only names nothing) by name, then those sharing a word
        of its name, best match first; never the product itself. NotFoundError for an unknown
        id. Read-only."""
        with self._uow_factory(read_only=True) as uow:
            product = uow.products.get(product_id)
            if product is None:
                msg = f"product {product_id} does not exist"
                raise NotFoundError(msg)
            own = {code.code.lstrip("0") for code in uow.product_codes.for_product(product_id)}
            own.discard("")
            barcode = {
                code.product_id for code in uow.product_codes.list() if code.code.lstrip("0") in own
            }
            named = uow.text_search.similar_product_ids(product.canonical_name)
            bought = {require_id(s.product.id): s for s in uow.products.summaries()}
        ranked = [
            *(pid for pid in bought if pid in barcode),
            *(pid for pid in named if pid in bought),
        ]
        found = [pid for pid in dict.fromkeys(ranked) if pid != product_id]
        return tuple(bought[pid] for pid in found[:MERGE_CANDIDATES])
