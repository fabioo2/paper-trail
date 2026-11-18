"""Applying discounts to the receipts stored before they were linked (``prices backfill``).

The attribution rule runs on each COMPLETED receipt's stored lines, newest first. A link already
stored is kept; the rule fills only the rest. A dry run writes nothing; applying sets the new
links and the discount of each linked item's price observation, one unit of work per receipt.
No amount, product or alias is written and no model is called, so a second run changes nothing.
A receipt whose unit fails stops the run; when applying, the receipts before it stay applied.
"""

from dataclasses import dataclass

import structlog

from paper_trail.domain.enums import ReceiptStatus
from paper_trail.domain.errors import DomainValidationError, NotFoundError
from paper_trail.domain.models import LineItem, ReceiptSummary, require_id
from paper_trail.domain.paging import MAX_PAGE_LIMIT, PageRequest
from paper_trail.domain.rules.discounts import Refusal, attribute_discounts
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BackfilledDiscount:
    """One DISCOUNT line of a stored receipt and what the backfill does with it: the ITEM line
    it reduces (None at receipt level, with the ``reason``), whether the link is not stored yet
    (``new_link``, set by ``apply``) and whether the item's price observation takes the discount
    (``price_updated``; False when the link is all that changes)."""

    receipt_id: int
    discount: LineItem
    item: LineItem | None
    reason: Refusal | None
    new_link: bool
    price_updated: bool


@dataclass(frozen=True, slots=True)
class PriceBackfill:
    """The discounts of the receipts looked at, newest receipt first, then by position."""

    discounts: tuple[BackfilledDiscount, ...]
    applied: bool

    @property
    def links_set(self) -> int:
        return sum(discount.new_link for discount in self.discounts)

    @property
    def prices_updated(self) -> int:
        return sum(discount.price_updated for discount in self.discounts)

    @property
    def changes(self) -> int:
        return self.links_set + self.prices_updated


class BackfillStoppedError(Exception):
    """A receipt's unit of work failed; when applying, the receipts before it stay applied."""

    def __init__(self, receipt_id: int, error: Exception) -> None:
        super().__init__(f"{type(error).__name__}: {error}")
        self.receipt_id = receipt_id


class BackfillPrices:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    def __call__(self, *, receipt_id: int | None = None, apply: bool = False) -> PriceBackfill:
        """Attribute the discounts of every COMPLETED receipt (or only ``receipt_id``) and,
        with ``apply``, store the outcome. NotFoundError for an unknown ``receipt_id``,
        DomainValidationError for one that is not COMPLETED.

        The scan only collects ids; each receipt is then read, attributed and (with ``apply``)
        written in its own unit of work, so a failure leaves earlier receipts applied and that
        one untouched: BackfillStoppedError names it. A dry run that stops has written nothing.
        """
        found: list[BackfilledDiscount] = []
        for completed in self._receipt_ids(receipt_id):
            try:
                found += self._receipt(completed, apply=apply)
            except Exception as error:
                raise BackfillStoppedError(completed, error) from error
        backfill = PriceBackfill(tuple(found), applied=apply)
        log.info(
            "prices.backfilled",
            applied=apply,
            discounts=len(found),
            links_set=backfill.links_set,
            prices_updated=backfill.prices_updated,
        )
        return backfill

    def _receipt(self, receipt_id: int, *, apply: bool) -> list[BackfilledDiscount]:
        with self._uow_factory(read_only=not apply) as uow:
            discounts = _backfilled(uow, receipt_id)
            if apply:
                _store(uow, discounts)
                uow.commit()
        return discounts

    def _receipt_ids(self, receipt_id: int | None) -> list[int]:
        """The COMPLETED receipts to look at, newest recorded first."""
        with self._uow_factory(read_only=True) as uow:
            if receipt_id is not None:
                receipt = uow.receipts.get(receipt_id)
                if receipt is None:
                    msg = f"unknown receipt {receipt_id}"
                    raise NotFoundError(msg)
                if receipt.status is not ReceiptStatus.COMPLETED:
                    msg = f"receipt {receipt_id} is not completed"
                    raise DomainValidationError(msg)
                return [receipt_id]
            summaries: list[ReceiptSummary] = []
            request = PageRequest(limit=MAX_PAGE_LIMIT)
            while True:
                page = uow.receipts.list(request, status=ReceiptStatus.COMPLETED)
                summaries += page.items
                if page.next_cursor is None:
                    newest = sorted(summaries, key=lambda s: (s.created_at, s.id), reverse=True)
                    return [summary.id for summary in newest]
                request = PageRequest(limit=MAX_PAGE_LIMIT, cursor=page.next_cursor)


def _backfilled(uow: UnitOfWork, receipt_id: int) -> list[BackfilledDiscount]:
    """The receipt's discounts as stored, attributed: its stored links are kept."""
    receipt = uow.receipts.get(receipt_id)
    if receipt is None or receipt.status is not ReceiptStatus.COMPLETED:
        return []
    attribution = attribute_discounts(receipt.items)
    by_position = {line.position: line for line in receipt.items}
    found = [
        BackfilledDiscount(
            receipt_id=receipt_id,
            discount=by_position[level.discount_position],
            item=None,
            reason=level.reason,
            new_link=False,
            price_updated=False,
        )
        for level in attribution.receipt_level
    ]
    for link in attribution.links:
        discount, item = by_position[link.discount_position], by_position[link.item_position]
        observation = uow.prices.for_line_item(require_id(item.id))
        found.append(
            BackfilledDiscount(
                receipt_id=receipt_id,
                discount=discount,
                item=item,
                reason=None,
                new_link=link.new,
                price_updated=observation is not None
                and observation.discount.amount_cents != discount.line_total.amount_cents,
            )
        )
    return sorted(found, key=lambda entry: entry.discount.position)


def _store(uow: UnitOfWork, discounts: list[BackfilledDiscount]) -> None:
    links = {
        entry.discount.position: entry.item.position
        for entry in discounts
        if entry.new_link and entry.item is not None
    }
    if links:
        uow.receipts.set_discount_links(discounts[0].receipt_id, links)
    for entry in discounts:
        if entry.price_updated and entry.item is not None:
            uow.prices.set_discount(require_id(entry.item.id), entry.discount.line_total)
