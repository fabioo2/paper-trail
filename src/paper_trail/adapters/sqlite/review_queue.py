"""``ReviewQueue`` on SQLite: ``review_items``; at most one OPEN item per receipt (a partial
unique index)."""

from collections.abc import Mapping

from sqlalchemy import Connection, func, insert, select, update

from paper_trail.adapters.sqlite.integrity import unique_violation_as_conflict
from paper_trail.adapters.sqlite.keyset import newest_first_page
from paper_trail.adapters.sqlite.mappers import review_from_row
from paper_trail.adapters.sqlite.schema import review_items
from paper_trail.domain.enums import ReviewReason, ReviewStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.models import JsonValue, ReviewItem
from paper_trail.domain.paging import Page, PageRequest
from paper_trail.ports.clock import Clock


class SqliteReviewQueue:
    """Implements ``paper_trail.ports.queues.ReviewQueue``."""

    def __init__(self, connection: Connection, clock: Clock) -> None:
        self._connection = connection
        self._clock = clock

    def open(
        self,
        receipt_id: int,
        reason: ReviewReason,
        details: Mapping[str, JsonValue],
        thread_id: str | None,
    ) -> ReviewItem:
        with unique_violation_as_conflict(f"receipt {receipt_id} already has an open review item"):
            row = (
                self._connection.execute(
                    insert(review_items)
                    .values(
                        receipt_id=receipt_id,
                        reason=reason,
                        details=dict(details),
                        status=ReviewStatus.OPEN,
                        thread_id=thread_id,
                        created_at=self._clock.now(),
                    )
                    .returning(review_items)
                )
                .mappings()
                .one()
            )
        return review_from_row(row)

    def get(self, review_id: int) -> ReviewItem | None:
        row = (
            self._connection.execute(select(review_items).where(review_items.c.id == review_id))
            .mappings()
            .one_or_none()
        )
        return None if row is None else review_from_row(row)

    def find_open(self, receipt_id: int) -> ReviewItem | None:
        row = (
            self._connection.execute(
                select(review_items).where(
                    review_items.c.receipt_id == receipt_id,
                    review_items.c.status == ReviewStatus.OPEN,
                )
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else review_from_row(row)

    def list_open(self, page: PageRequest) -> Page[ReviewItem]:
        query = select(review_items).where(review_items.c.status == ReviewStatus.OPEN)
        return newest_first_page(self._connection, query, review_items, page, review_from_row)

    def resolve(self, review_id: int) -> ReviewItem:
        return self._close(review_id, ReviewStatus.RESOLVED)

    def dismiss(self, review_id: int) -> ReviewItem:
        return self._close(review_id, ReviewStatus.DISMISSED)

    def open_count(self) -> int:
        count: int = self._connection.execute(
            select(func.count())
            .select_from(review_items)
            .where(review_items.c.status == ReviewStatus.OPEN)
        ).scalar_one()
        return count

    def _close(self, review_id: int, status: ReviewStatus) -> ReviewItem:
        item = self.get(review_id)
        if item is None:
            msg = f"review item {review_id} does not exist"
            raise NotFoundError(msg)
        if item.status is not ReviewStatus.OPEN:
            msg = f"review item {review_id} is already {item.status}"
            raise ConflictError(msg)
        row = (
            self._connection.execute(
                update(review_items)
                .where(review_items.c.id == review_id)
                .values(status=status, resolved_at=self._clock.now())
                .returning(review_items)
            )
            .mappings()
            .one()
        )
        return review_from_row(row)
