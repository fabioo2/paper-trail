"""In-memory repositories (implement ``paper_trail.ports.repositories``) over a ``FakeState``."""

from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from paper_trail.domain.enums import AliasSource, CorrectionKind, ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.models import (
    Correction,
    LineItem,
    PriceObservation,
    Product,
    ProductAlias,
    Receipt,
    ReceiptSummary,
)
from paper_trail.domain.paging import Page, PageRequest
from paper_trail.ports.clock import Clock
from tests.fakes.state import FakeState, newest_first_page
from tests.support import require_id

if TYPE_CHECKING:  # agent core slice C; a type only, so the fakes import before it exists
    from paper_trail.domain.models import ItemLink


class FakeReceiptRepository:
    def __init__(self, state: FakeState, clock: Clock) -> None:
        self._state = state
        self._clock = clock

    def add(self, receipt: Receipt) -> Receipt:
        sha256 = receipt.image_sha256
        if sha256 is not None and self.find_by_image_sha256(sha256) is not None:
            msg = "a receipt already has this image"
            raise ConflictError(msg)
        stored = replace(
            receipt, id=self._state.next_id("receipts"), items=self._new_items(receipt.items)
        )
        self._state.receipts[require_id(stored.id)] = stored
        return stored

    def get(self, receipt_id: int) -> Receipt | None:
        return self._state.receipts.get(receipt_id)

    def list(
        self,
        page: PageRequest,
        *,
        status: ReceiptStatus | None = None,
        store_key: str | None = None,
    ) -> Page[ReceiptSummary]:
        rows = [
            receipt
            for receipt in self._state.receipts.values()
            if (status is None or receipt.status is status)
            and (store_key is None or receipt.store_key == store_key)
        ]
        return newest_first_page(rows, page, _summary)

    def update_status(self, receipt_id: int, status: ReceiptStatus, stage: ProcessingStage) -> None:
        current = self._existing(receipt_id)
        self._state.receipts[receipt_id] = replace(
            current, status=status, stage=stage, updated_at=self._clock.now()
        )

    def replace_contents(self, receipt: Receipt) -> Receipt:
        receipt_id = require_id(receipt.id)
        current = self._existing(receipt_id)
        # Like ON DELETE CASCADE: the replaced lines take their price observations with them.
        replaced = {item.id for item in current.items}
        for price_id, row in list(self._state.prices.items()):
            if row.line_item_id in replaced:
                del self._state.prices[price_id]
        stored = replace(
            receipt,
            items=self._new_items(receipt.items),
            created_at=current.created_at,
            updated_at=self._clock.now(),
        )
        self._state.receipts[receipt_id] = stored
        return stored

    def link_items(self, receipt_id: int, links: "Sequence[ItemLink]") -> Receipt:
        current = self._existing(receipt_id)
        by_position = {item.position: item for item in current.items}
        for link in links:
            if link.position not in by_position:
                msg = f"receipt {receipt_id} has no line at position {link.position}"
                raise NotFoundError(msg)
            by_position[link.position] = replace(
                by_position[link.position],
                product_id=link.product_id,
                match_source=link.match_source,
            )
        stored = replace(
            current,
            items=tuple(by_position[item.position] for item in current.items),
            updated_at=self._clock.now(),
        )
        self._state.receipts[receipt_id] = stored
        return stored

    def find_by_image_sha256(self, sha256: str) -> Receipt | None:
        return next(
            (
                receipt
                for receipt in self._state.receipts.values()
                if receipt.image_sha256 == sha256
            ),
            None,
        )

    def _existing(self, receipt_id: int) -> Receipt:
        current = self._state.receipts.get(receipt_id)
        if current is None:
            msg = f"receipt {receipt_id} does not exist"
            raise NotFoundError(msg)
        return current

    def _new_items(self, items: Sequence[LineItem]) -> tuple[LineItem, ...]:
        ordered = sorted(items, key=lambda item: item.position)
        return tuple(replace(item, id=self._state.next_id("line_items")) for item in ordered)


def _summary(receipt: Receipt) -> ReceiptSummary:
    return ReceiptSummary(
        id=require_id(receipt.id),
        status=receipt.status,
        store_name=receipt.store_name,
        purchased_on=receipt.purchased_on,
        total=receipt.total,
        item_count=len(receipt.items),
        created_at=receipt.created_at,
    )


class FakeProductRepository:
    def __init__(self, state: FakeState) -> None:
        self._state = state

    def add(self, product: Product) -> Product:
        stored = replace(product, id=self._state.next_id("products"))
        self._state.products[require_id(stored.id)] = stored
        return stored

    def get(self, product_id: int) -> Product | None:
        return self._state.products.get(product_id)

    def update(self, product: Product) -> Product:
        """Slice D: replace a stored product's fields (``created_at`` is kept)."""
        stored = self._state.products.get(require_id(product.id))
        if stored is None:
            msg = f"product {product.id} does not exist"
            raise NotFoundError(msg)
        updated = replace(product, created_at=stored.created_at)
        self._state.products[require_id(updated.id)] = updated
        return updated

    def find_by_alias(self, alias: str, store_key: str | None) -> Product | None:
        keys = (store_key, None) if store_key is not None else (None,)
        for key in keys:
            match = self._alias(alias, key)
            if match is not None:
                return self._state.products.get(match.product_id)
        return None

    def add_alias(self, alias: ProductAlias) -> ProductAlias:
        existing = self._alias(alias.alias, alias.store_key)
        if existing is None:
            stored = replace(alias, id=self._state.next_id("product_aliases"))
        elif _outranks(existing.source, alias.source):
            return existing
        else:
            stored = replace(alias, id=existing.id)
        self._state.aliases[require_id(stored.id)] = stored
        return stored

    def aliases(self, product_id: int) -> tuple[ProductAlias, ...]:
        return tuple(
            alias
            for _, alias in sorted(self._state.aliases.items())
            if alias.product_id == product_id
        )

    def _alias(self, alias: str, store_key: str | None) -> ProductAlias | None:
        return next(
            (
                row
                for row in self._state.aliases.values()
                if row.alias == alias and row.store_key == store_key
            ),
            None,
        )


def _outranks(existing: AliasSource, incoming: AliasSource) -> bool:
    """A user's correction is never overwritten by a machine-made alias."""
    return existing is AliasSource.USER_CORRECTION and incoming is not AliasSource.USER_CORRECTION


class FakePriceRepository:
    def __init__(self, state: FakeState) -> None:
        self._state = state

    def add_many(self, observations: Sequence[PriceObservation]) -> tuple[PriceObservation, ...]:
        taken = {row.line_item_id for row in self._state.prices.values()}
        incoming = [row.line_item_id for row in observations]
        if taken.intersection(incoming) or len(set(incoming)) != len(incoming):
            msg = "a line item has at most one price observation"
            raise ConflictError(msg)
        stored = tuple(
            replace(row, id=self._state.next_id("price_observations")) for row in observations
        )
        for row in stored:
            self._state.prices[require_id(row.id)] = row
        return stored

    def history(self, product_id: int) -> tuple[PriceObservation, ...]:
        rows = [row for row in self._state.prices.values() if row.product_id == product_id]
        return tuple(sorted(rows, key=lambda row: (row.observed_on, require_id(row.id))))

    def reassign(self, line_item_id: int, product_id: int) -> PriceObservation | None:
        for price_id, row in self._state.prices.items():
            if row.line_item_id == line_item_id:
                moved = replace(row, product_id=product_id)
                self._state.prices[price_id] = moved
                return moved
        return None


class FakeCorrectionRepository:
    def __init__(self, state: FakeState) -> None:
        self._state = state

    def add(self, correction: Correction) -> Correction:
        stored = replace(correction, id=self._state.next_id("corrections"))
        self._state.corrections[require_id(stored.id)] = stored
        return stored

    def latest(
        self, kind: CorrectionKind, subject: str, store_key: str | None
    ) -> Correction | None:
        rows = [
            row
            for row in self._state.corrections.values()
            if row.kind is kind and row.subject == subject and row.store_key == store_key
        ]
        return max(rows, key=lambda row: (row.created_at, require_id(row.id)), default=None)
