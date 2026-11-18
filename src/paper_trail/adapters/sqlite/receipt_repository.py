"""``ReceiptRepository`` on SQLite: ``receipts`` plus their ``line_items``."""

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime

from sqlalchemy import (
    ColumnElement,
    Connection,
    Integer,
    Label,
    Select,
    TextualSelect,
    and_,
    column,
    delete,
    false,
    func,
    insert,
    or_,
    select,
    text,
    update,
)

from paper_trail.adapters.sqlite.integrity import unique_violation_as_conflict
from paper_trail.adapters.sqlite.keyset import newest_purchase_first_page
from paper_trail.adapters.sqlite.mappers import (
    line_item_from_row,
    line_item_values,
    receipt_from_row,
    receipt_spend_from_row,
    receipt_summary_from_row,
    receipt_values,
)
from paper_trail.adapters.sqlite.schema import line_items, receipts
from paper_trail.adapters.sqlite.text_search import fts_query
from paper_trail.domain.enums import FailureReason, LineKind, ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.models import (
    ItemLink,
    LineItem,
    ProductLine,
    Receipt,
    ReceiptMatch,
    ReceiptSpend,
    ReceiptSummary,
)
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
        query = _summaries()
        if status is not None:
            query = query.where(receipts.c.status == status)
        if store_key is not None:
            query = query.where(receipts.c.store_key == store_key)
        return newest_purchase_first_page(
            self._connection, query, receipts, page, receipt_summary_from_row
        )

    def count(self, status: ReceiptStatus) -> int:
        count: int = self._connection.execute(
            select(func.count()).select_from(receipts).where(receipts.c.status == status)
        ).scalar_one()
        return count

    def spending(self, start: date, end: date) -> tuple[ReceiptSpend, ...]:
        rows = self._connection.execute(
            select(
                receipts.c.store_key,
                receipts.c.store_name,
                receipts.c.purchased_on,
                receipts.c.currency,
                receipts.c.total_cents,
                _savings(),
            )
            .where(
                receipts.c.status == ReceiptStatus.COMPLETED,
                receipts.c.purchased_on.between(start, end),
            )
            .order_by(receipts.c.purchased_on, receipts.c.id)
        ).mappings()
        return tuple(receipt_spend_from_row(row) for row in rows)

    def matching(self, query: str, *, limit: int) -> tuple[ReceiptMatch, ...]:
        match = fts_query(query)
        matched_line = and_(
            line_items.c.kind == LineKind.ITEM,
            or_(
                line_items.c.id.in_(_fts_ids("line_items_fts", match)),
                line_items.c.product_id.in_(_fts_ids("products_fts", match)),
            )
            if match is not None
            else false(),
        )
        candidates = _summaries().where(
            or_(
                receipts.c.store_name.contains(query, autoescape=True),
                receipts.c.id.in_(select(line_items.c.receipt_id).where(matched_line)),
            )
        )
        found = newest_purchase_first_page(
            self._connection,
            candidates,
            receipts,
            PageRequest(limit=limit),
            receipt_summary_from_row,
        ).items
        lines: dict[int, list[str]] = {summary.id: [] for summary in found}
        for receipt_id, description in self._connection.execute(
            select(line_items.c.receipt_id, line_items.c.description)
            .where(matched_line, line_items.c.receipt_id.in_(lines))
            .order_by(line_items.c.receipt_id, line_items.c.position)
        ):
            lines[receipt_id].append(description)
        return tuple(ReceiptMatch(summary, tuple(lines[summary.id])) for summary in found)

    def update_status(
        self,
        receipt_id: int,
        status: ReceiptStatus,
        stage: ProcessingStage,
        *,
        failure_reason: FailureReason | None = None,
    ) -> None:
        self._update(
            receipt_id, {"status": status, "stage": stage, "failure_reason": failure_reason}
        )

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

    def set_discount_links(self, receipt_id: int, links: Mapping[int, int]) -> Receipt:
        current = self.get(receipt_id)
        if current is None:
            msg = f"receipt {receipt_id} does not exist"
            raise NotFoundError(msg)
        by_position = {item.position: item for item in current.items}
        changed: dict[int, int] = {}
        for position, target in links.items():
            for wanted in (position, target):
                if wanted not in by_position:
                    msg = f"receipt {receipt_id} has no line at position {wanted}"
                    raise NotFoundError(msg)
            line = by_position[position]
            if line.applies_to_position not in {None, target}:
                msg = (
                    f"receipt {receipt_id} line at position {position} already applies to"
                    f" position {line.applies_to_position}"
                )
                raise ConflictError(msg)
            if line.applies_to_position is None:
                changed[position] = target
                by_position[position] = replace(line, applies_to_position=target)
        if not changed:
            return current
        linked = replace(current, items=tuple(by_position[item.position] for item in current.items))
        for position, target in changed.items():
            self._connection.execute(
                update(line_items)
                .where(line_items.c.id == by_position[position].id)
                .values(applies_to_position=target)
            )
        _, updated_at = self._update(receipt_id, {})
        return replace(linked, updated_at=updated_at)

    def item_lines(self, product_id: int) -> tuple[ProductLine, ...]:
        rows = self._connection.execute(
            select(
                line_items,
                receipts.c.store_key.label("receipt_store_key"),
                receipts.c.purchased_on.label("receipt_purchased_on"),
                receipts.c.currency.label("receipt_currency"),
            )
            .join(receipts, receipts.c.id == line_items.c.receipt_id)
            .where(line_items.c.product_id == product_id, line_items.c.kind == LineKind.ITEM)
            .order_by(
                receipts.c.purchased_on.is_(None),
                receipts.c.purchased_on,
                receipts.c.id,
                line_items.c.position,
            )
        ).mappings()
        return tuple(
            ProductLine(
                receipt_id=row["receipt_id"],
                store_key=row["receipt_store_key"],
                purchased_on=row["receipt_purchased_on"],
                line=line_item_from_row(row, row["receipt_currency"]),
            )
            for row in rows
        )

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


def _savings() -> Label[int]:
    """What a receipt's DISCOUNT lines took off (>= 0), as ``savings_cents``."""
    return (
        select(-func.coalesce(func.sum(line_items.c.line_total_cents), 0))
        .where(
            line_items.c.receipt_id == receipts.c.id,
            line_items.c.kind == LineKind.DISCOUNT,
        )
        .scalar_subquery()
        .label("savings_cents")
    )


def _summaries() -> Select[tuple[object, ...]]:
    item_count = (
        select(func.count())
        .where(line_items.c.receipt_id == receipts.c.id, line_items.c.kind == LineKind.ITEM)
        .scalar_subquery()
        .label("item_count")
    )
    return select(receipts, item_count, _savings())


def _fts_ids(table: str, match: str) -> TextualSelect:
    """The rowids of ``table`` (an FTS5 table of the schema) matching the FTS5 query."""
    return (
        text(f"SELECT rowid FROM {table} WHERE {table} MATCH :{table}")  # noqa: S608 - a constant
        .bindparams(**{table: match})
        .columns(column("rowid", Integer))
    )
