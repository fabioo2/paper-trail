"""A person corrects the product of a receipt line, and it is remembered (PLAN 6, S4-AC-06).

The correction is matching's memory: the next line with the same alias at the same store is
linked to the corrected product before any alias search (``find_product``). No model
is called.
"""

from dataclasses import dataclass, replace
from datetime import datetime

import structlog

from paper_trail.application.matching import attach_code
from paper_trail.application.products import new_product, person_named, remember_alias
from paper_trail.domain.enums import (
    AliasSource,
    Category,
    CodeSource,
    CorrectionKind,
    FieldSource,
    LineKind,
    MatchSource,
)
from paper_trail.domain.errors import DomainValidationError, NotFoundError
from paper_trail.domain.models import (
    CodeIdentity,
    Correction,
    ItemLink,
    LineItem,
    Product,
    Receipt,
    require_id,
)
from paper_trail.domain.rules.item_codes import parse_item_code
from paper_trail.domain.rules.normalization import normalize_alias
from paper_trail.ports.clock import Clock
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Subject:
    """What a correction is remembered under: the line's alias at the receipt's store."""

    alias: str
    store_key: str
    receipt_id: int
    line_item_id: int


class CorrectLineItem:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def __call__(
        self,
        receipt_id: int,
        position: int,
        *,
        product_id: int | None = None,
        new_product_name: str | None = None,
        category: Category | None = None,
    ) -> LineItem:
        """Relink the line at ``position`` (0-based, as stored) to ``product_id`` or to a new
        product named ``new_product_name`` (with ``category``), in one unit of work: the line's
        source becomes MEMORY, its price observation moves along, and a PRODUCT_LINK correction
        and a USER_CORRECTION alias are remembered (a CATEGORY correction too, with
        ``category``, which an existing product also takes, stamped ``person``). When the
        line's raw text starts with a product code, the code now names the target (source
        ``correction``, replacing another product's mapping; logged ``catalog.learned``).
        DomainValidationError unless exactly one target is given; NotFoundError for an unknown
        receipt, position or product. Returns the corrected line."""
        if (product_id is None) == (new_product_name is None):
            msg = "give exactly one of a product id and a new product name"
            raise DomainValidationError(msg)
        now = self._clock.now()
        with self._uow_factory() as uow:
            receipt, line = _line(uow, receipt_id, position)
            subject = _Subject(
                normalize_alias(line.raw_text), receipt.store_key, receipt_id, require_id(line.id)
            )
            if product_id is None:
                target = require_id(
                    new_product(
                        uow,
                        str(new_product_name),
                        category,
                        now,
                        field_sources=person_named(category),
                    ).id
                )
            else:
                existing = uow.products.get(product_id)
                if existing is None:
                    msg = f"product {product_id} does not exist"
                    raise NotFoundError(msg)
                target = product_id
                if category is not None:
                    _categorize(uow, existing, category)
            uow.receipts.link_items(receipt_id, [ItemLink(position, target, MatchSource.MEMORY)])
            uow.prices.reassign(subject.line_item_id, target)
            old = None if line.product_id is None else str(line.product_id)
            _remember(uow, CorrectionKind.PRODUCT_LINK, subject, old=old, new=str(target), now=now)
            remember_alias(
                uow,
                target,
                subject.alias,
                store_key=subject.store_key,
                source=AliasSource.USER_CORRECTION,
                now=now,
            )
            if category is not None:
                _remember(
                    uow, CorrectionKind.CATEGORY, subject, old=None, new=category.value, now=now
                )
            learned = _learn_code(uow, line, receipt.store_key, target, now)
            uow.commit()
        if learned is not None:
            log.info(
                "catalog.learned",
                scheme=learned.scheme.value,
                store_key=learned.store_key,
                code=learned.code,
                product_id=target,
            )
        log.info("line_item.corrected", receipt_id=receipt_id, position=position, product_id=target)
        return replace(line, product_id=target, match_source=MatchSource.MEMORY)


def _categorize(uow: UnitOfWork, product: Product, category: Category) -> None:
    """A person's category for an existing product, stamped ``person``."""
    uow.products.update(
        replace(
            product,
            category=category,
            field_sources={**product.field_sources, "category": FieldSource.PERSON},
        )
    )


def _learn_code(
    uow: UnitOfWork, line: LineItem, store_key: str, product_id: int, now: datetime
) -> CodeIdentity | None:
    """The item line's code now names the corrected product, replacing another product's
    mapping; None when the line is no item (a discount's reference is not its code) or has no
    product code."""
    if line.kind is not LineKind.ITEM:
        return None
    parsed = parse_item_code(line.raw_text, store_key)
    identity = None if parsed is None else parsed.identity_at(store_key)
    if identity is None:
        return None
    attach_code(uow, identity, product_id, CodeSource.CORRECTION, now)
    return identity


def _line(uow: UnitOfWork, receipt_id: int, position: int) -> tuple[Receipt, LineItem]:
    receipt = uow.receipts.get(receipt_id)
    if receipt is None:
        msg = f"receipt {receipt_id} does not exist"
        raise NotFoundError(msg)
    line = _at(receipt, position)
    if line is None:
        msg = f"receipt {receipt_id} has no line at position {position}"
        raise NotFoundError(msg)
    return receipt, line


def _at(receipt: Receipt, position: int) -> LineItem | None:
    return next((item for item in receipt.items if item.position == position), None)


def _remember(  # noqa: PLR0913 - one correction's fields
    uow: UnitOfWork,
    kind: CorrectionKind,
    subject: _Subject,
    *,
    old: str | None,
    new: str,
    now: datetime,
) -> None:
    uow.corrections.add(
        Correction(
            id=None,
            kind=kind,
            subject=subject.alias,
            old_value=old,
            new_value=new,
            store_key=subject.store_key,
            receipt_id=subject.receipt_id,
            line_item_id=subject.line_item_id,
            created_at=now,
        )
    )
