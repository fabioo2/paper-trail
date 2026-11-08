"""ReviewQueue contract (S1-AC-10, S1-AC-15), for the fake and the SQLite adapter.

Pinned (docs/tests/stage1.md): ``list_open`` pages newest first with the receipts' cursor scheme;
``resolve``/``dismiss`` of an unknown id raise NotFoundError.

Pinned (docs/tests/agent-core-B.md, slice A review note 4): ``find_open(receipt_id)`` returns the
receipt's OPEN item or None, so the ``open_review`` tool can be idempotent per receipt and thread.
"""

from collections.abc import Mapping
from datetime import timedelta

import pytest

from paper_trail.domain.enums import ReviewReason, ReviewStatus
from paper_trail.domain.errors import ConflictError, NotFoundError
from paper_trail.domain.models import JsonValue, ReviewItem
from paper_trail.domain.paging import PageRequest
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.factories import receipt
from tests.fakes.clock import FakeClock
from tests.storage import in_unit
from tests.support import require_id

DETAILS: dict[str, JsonValue] = {
    "issues": [
        {
            "code": "line_math",
            "message": "line 3: 2 x 4.99 = 9.98 but line total is 9.89",
            "expected": {"amount_cents": 998, "currency": "CAD"},
            "actual": {"amount_cents": 989, "currency": "CAD"},
            "position": 2,
        }
    ],
    "confidence": 0.42,
    "retryable": False,
    "note": None,
    "unicode": "crème brûlée ✓",
}


def _receipt_id(uow_factory: UnitOfWorkFactory) -> int:
    return require_id(in_unit(uow_factory, lambda uow: uow.receipts.add(receipt())).id)


def _open(
    uow_factory: UnitOfWorkFactory,
    receipt_id: int,
    details: Mapping[str, JsonValue] | None = None,
    thread_id: str | None = "thread-1",
) -> ReviewItem:
    return in_unit(
        uow_factory,
        lambda uow: uow.reviews.open(
            receipt_id, ReviewReason.VALIDATION_FAILED, details or DETAILS, thread_id
        ),
    )


def _get(uow_factory: UnitOfWorkFactory, item: ReviewItem) -> ReviewItem | None:
    with uow_factory() as uow:
        return uow.reviews.get(require_id(item.id))


def _close(uow_factory: UnitOfWorkFactory, how: str, review_id: int) -> ReviewItem:
    def close(uow: UnitOfWork) -> ReviewItem:
        if how == "resolve":
            return uow.reviews.resolve(review_id)
        return uow.reviews.dismiss(review_id)

    return in_unit(uow_factory, close)


def test_s1_ac_15_open_creates_an_open_item(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-15: open() stores an OPEN item stamped by the clock."""
    receipt_id = _receipt_id(uow_factory)

    item = _open(uow_factory, receipt_id)

    assert item.id is not None
    assert (item.receipt_id, item.reason, item.status, item.thread_id) == (
        receipt_id,
        ReviewReason.VALIDATION_FAILED,
        ReviewStatus.OPEN,
        "thread-1",
    )
    assert item.created_at == fake_clock.now()
    assert item.resolved_at is None
    assert _get(uow_factory, item) == item


def test_s1_ac_15_details_json_round_trips(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-15: nested lists, objects, floats, booleans, null and unicode survive."""
    item = _open(uow_factory, _receipt_id(uow_factory), thread_id=None)

    loaded = _get(uow_factory, item)

    assert loaded is not None
    assert dict(loaded.details) == DETAILS
    assert loaded.thread_id is None


def test_s1_ac_15_one_open_item_per_receipt(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-15: a second OPEN item for the same receipt raises ConflictError."""
    receipt_id = _receipt_id(uow_factory)
    _open(uow_factory, receipt_id)

    with pytest.raises(ConflictError):
        _open(uow_factory, receipt_id)

    other = _open(uow_factory, _receipt_id(uow_factory))
    assert other.id is not None


@pytest.mark.parametrize(
    ("close", "status"), [("resolve", ReviewStatus.RESOLVED), ("dismiss", ReviewStatus.DISMISSED)]
)
def test_s1_ac_15_resolve_and_dismiss_close_the_item(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock, close: str, status: ReviewStatus
) -> None:
    """S1-AC-15: resolve/dismiss set the status and resolved_at; a second close is a conflict."""
    receipt_id = _receipt_id(uow_factory)
    item = _open(uow_factory, receipt_id)
    review_id = require_id(item.id)
    fake_clock.advance(timedelta(minutes=7))

    closed = _close(uow_factory, close, review_id)

    assert closed.status is status
    assert closed.resolved_at == fake_clock.now()
    assert closed.created_at == item.created_at
    assert _get(uow_factory, item) == closed
    for again in ("resolve", "dismiss"):
        with pytest.raises(ConflictError):
            _close(uow_factory, again, review_id)
    reopened = _open(uow_factory, receipt_id)
    assert reopened.id != item.id


@pytest.mark.parametrize("close", ["resolve", "dismiss"])
def test_s1_ac_15_closing_an_unknown_item_raises_not_found(
    uow_factory: UnitOfWorkFactory, close: str
) -> None:
    """Pinned: resolve/dismiss of an unknown id raise NotFoundError."""
    with pytest.raises(NotFoundError):
        _close(uow_factory, close, 999)


def test_s1_ac_10_get_unknown_review_is_none(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: get() of an unknown id is None."""
    with uow_factory() as uow:
        assert uow.reviews.get(999) is None


def test_s1_ac_10_list_open_pages_newest_first_and_counts(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-10: list_open shows only OPEN items, newest first, paged; open_count agrees."""
    items = []
    for _ in range(4):
        items.append(_open(uow_factory, _receipt_id(uow_factory)))
        fake_clock.advance(1)
    _close(uow_factory, "dismiss", require_id(items[1].id))

    with uow_factory() as uow:
        first = uow.reviews.list_open(PageRequest(limit=2))
        assert first.next_cursor is not None
        rest = uow.reviews.list_open(PageRequest(limit=2, cursor=first.next_cursor))
        count = uow.reviews.open_count()

    assert [item.id for item in first.items + rest.items] == [items[3].id, items[2].id, items[0].id]
    assert rest.next_cursor is None
    assert count == 3


def test_find_open_returns_the_receipts_open_item(uow_factory: UnitOfWorkFactory) -> None:
    """Agent core slice B (review note 4): the OPEN item of a receipt, whatever its thread."""
    receipt_id = _receipt_id(uow_factory)
    other_id = _receipt_id(uow_factory)
    item = _open(uow_factory, receipt_id, thread_id="receipt-1")

    with uow_factory(read_only=True) as uow:
        found = uow.reviews.find_open(receipt_id)
        nothing = uow.reviews.find_open(other_id)
        unknown = uow.reviews.find_open(999)

    assert found == item
    assert (nothing, unknown) == (None, None)


@pytest.mark.parametrize("close", ["resolve", "dismiss"])
def test_find_open_ignores_closed_items(uow_factory: UnitOfWorkFactory, close: str) -> None:
    receipt_id = _receipt_id(uow_factory)
    item = _open(uow_factory, receipt_id)
    _close(uow_factory, close, require_id(item.id))

    with uow_factory(read_only=True) as uow:
        assert uow.reviews.find_open(receipt_id) is None
