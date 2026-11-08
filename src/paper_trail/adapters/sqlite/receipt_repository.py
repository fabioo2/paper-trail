"""``ReceiptRepository`` on SQLite: ``receipts`` plus their ``line_items``."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime

from sqlalchemy import ColumnElement, Connection, delete, func, insert, select, update

from paper_trail.adapters.sqlite.integrity import unique_violation_as_conflict
from paper_trail.adapters.sqlite.keyset import newest_first_page
from paper_trail.adapters.sqlite.mappers import (
    line_item_values,
    receipt_from_row,
    receipt_summary_from_row,
    receipt_values,
)
from paper_trail.adapters.sqlite.schema import line_items, receipts
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import ItemLink, LineItem, Receipt, ReceiptSummary
from paper_trail.domain.paging import Page, PageRequest
from paper_trail.ports.clock import Clock


class SqliteReceiptRepository:
    """Implements ``paper_trail.ports.repositories.ReceiptRepository``."""

    def __init__(self, connection: Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock

    def add(self, receipt: Receipt) -> Receipt:
        with unique_violation_as_conflict("a receipt already has this image"):
            receipt_id = self._connection.execute(
                insert(receipts).values(receipt_values(receipt)).returning(receipts.c.id)
            ).scalar_one()
        return replace(receipt, id=receipt_id, items=self._insert_items(receipt_id, receipt.items))

    def get(self, receipt_id: int) -> Receipt | None:
        return self._one(receipts.c.id == receipt_id)

    def find_by_image_sha256(self, sha256: str) -> Receipt | None:
        return self._one(receipts.c.image_sha256 == sha256)

    def _one(self, condition: ColumnElement[bool]) -> Receipt | None:
        row = self._connection.execute(select(receipts).where(condition)).mappings().one_or_none()
        if row is None:
            return None
        items = (
            self._connection.execute(
                select(line_items)
                .where(line_items.c.receipt_id == row["id"])
                .order_by(line_items.c.position)
            )
            .mappings()
            .all()
        )
        return receipt_from_row(row, items)

    def list(
        self,
        page: PageRequest,
        *,
        status: ReceiptStatus | None = None,
        store_key: str | None = None,
    ) -> Page[ReceiptSummary]:
        item_count = (
            select(func.count())
            .where(line_items.c.receipt_id == receipts.c.id)
            .scalar_subquery()
            .label("item_count")
        )
        query = select(receipts, item_count)
        if status is not None:
            query = query.where(receipts.c.status == status)
        if store_key is not None:
            query = query.where(receipts.c.store_key == store_key)
        return newest_first_page(self._connection, query, receipts, page, receipt_summary_from_row)

    def update_status(self, receipt_id: int, status: ReceiptStatus, stage: ProcessingStage) -> None:
        self._update(receipt_id, {"status": status, "stage": stage})

    def replace_contents(self, receipt: Receipt) -> Receipt:
        if receipt.id is None:
            msg = "only a stored receipt (with an id) can be replaced"
            raise ValueError(msg)
        values = receipt_values(receipt)
        del values["created_at"]  # the original time of recording is kept
        created_at, updated_at = self._update(receipt.id, values)
        self._connection.execute(delete(line_items).where(line_items.c.receipt_id == receipt.id))
        items = self._insert_items(receipt.id, receipt.items)
        return replace(receipt, items=items, created_at=created_at, updated_at=updated_at)

    def link_items(self, receipt_id: int, links: Sequence[ItemLink]) -> Receipt:
        current = self.get(receipt_id)
        if current is None:
            msg = f"receipt {receipt_id} does not exist"
            raise NotFoundError(msg)
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
        for link in links:
            self._connection.execute(
                update(line_items)
                .where(line_items.c.id == by_position[link.position].id)
                .values(product_id=link.product_id, match_source=link.match_source)
            )
        _, updated_at = self._update(receipt_id, {})
        items = tuple(by_position[item.position] for item in current.items)
        return replace(current, items=items, updated_at=updated_at)

    def _update(self, receipt_id: int, values: dict[str, object]) -> tuple[datetime, datetime]:
        """Apply ``values`` and stamp updated_at; returns (created_at, updated_at)."""
        updated_at = self._clock.now()
        created_at = self._connection.execute(
            update(receipts)
            .where(receipts.c.id == receipt_id)
            .values({**values, "updated_at": updated_at})
            .returning(receipts.c.created_at)
        ).scalar_one_or_none()
        if created_at is None:
            msg = f"receipt {receipt_id} does not exist"
            raise NotFoundError(msg)
        return created_at, updated_at

    def _insert_items(self, receipt_id: int, items: Sequence[LineItem]) -> tuple[LineItem, ...]:
        ordered = sorted(items, key=lambda item: item.position)
        if not ordered:
            return ()
        ids = self._connection.execute(
            insert(line_items).returning(line_items.c.id, sort_by_parameter_order=True),
            [line_item_values(item, receipt_id) for item in ordered],
        ).scalars()
        return tuple(replace(item, id=item_id) for item, item_id in zip(ordered, ids, strict=True))
