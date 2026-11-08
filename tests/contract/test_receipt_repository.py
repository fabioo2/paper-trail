"""ReceiptRepository contract (S1-AC-10, S1-AC-11), for the fake and the SQLite adapter."""

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from paper_trail.domain.enums import Category, LineKind, ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import DomainValidationError, NotFoundError
from paper_trail.domain.models import Receipt, ReceiptSummary
from paper_trail.domain.money import Money
from paper_trail.domain.paging import PageRequest
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.factories import at, cad, line_item, receipt
from tests.fakes.clock import FakeClock
from tests.storage import in_unit


def _add(uow_factory: UnitOfWorkFactory, value: Receipt) -> Receipt:
    return in_unit(uow_factory, lambda uow: uow.receipts.add(value))


def _get(uow_factory: UnitOfWorkFactory, receipt_id: int) -> Receipt | None:
    with uow_factory() as uow:
        return uow.receipts.get(receipt_id)


def _without_ids(value: Receipt) -> Receipt:
    return replace(value, id=None, items=tuple(replace(item, id=None) for item in value.items))


def _every_field_receipt() -> Receipt:
    """Every column holds a distinctive value: fractional quantity, negative money, no product."""
    return receipt(
        status=ReceiptStatus.NEEDS_REVIEW,
        stage=ProcessingStage.NEEDS_REVIEW,
        store_name="Farm Boy #12",
        store_key="farm boy",
        purchased_on=date(2024, 2, 29),
        currency="USD",
        subtotal=Money(1013, "USD"),
        tax=Money(0, "USD"),
        total=Money(1013, "USD"),
        payment_last4="0042",
        ocr_text="FARM BOY\nBANANAS 0.452 KG @ 10.99\n",
        image_path="2026/11/receipt-1.jpg",
        items=(
            line_item(position=0, kind=LineKind.ITEM, raw_text="BANANAS",
                      description="Bananas", quantity=Decimal("0.452"),
                      unit_price=Money(1099, "USD"), line_total=Money(497, "USD"),
                      category=Category.PRODUCE),
            line_item(position=1, kind=LineKind.DISCOUNT, raw_text="COUPON",
                      description="Coupon", quantity=Decimal("1"), unit_price=None,
                      line_total=Money(-100, "USD"), category=None),
            line_item(position=2, kind=LineKind.DEPOSIT, raw_text="DEP",
                      description="Deposit", quantity=Decimal("12.000"),
                      unit_price=Money(10, "USD"), line_total=Money(120, "USD")),
            line_item(position=3, kind=LineKind.FEE, raw_text="FEE", description="Fee",
                      quantity=Decimal("1"), unit_price=None, line_total=Money(496, "USD")),
        ),
        created_at=at(0).replace(microsecond=123456),
        updated_at=at(5).replace(microsecond=654321),
    )  # fmt: skip


def test_s1_ac_10_add_assigns_ids_to_receipt_and_items(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: add() returns the receipt with ids on it and on every line item."""
    saved = _add(uow_factory, receipt())

    assert saved.id is not None
    assert all(item.id is not None for item in saved.items)
    assert len({item.id for item in saved.items}) == len(saved.items)
    assert _without_ids(saved) == receipt()


def test_s1_ac_11_add_then_get_round_trips_every_field(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11: Money, Decimal quantity, dates, tz-aware datetimes, enums survive exactly."""
    original = _every_field_receipt()
    saved = _add(uow_factory, original)
    assert saved.id is not None

    loaded = _get(uow_factory, saved.id)

    assert loaded == saved
    assert loaded is not None
    assert _without_ids(loaded) == original
    assert [str(item.quantity) for item in loaded.items] == ["0.452", "1", "12.000", "1"]
    assert all(isinstance(item.quantity, Decimal) for item in loaded.items)
    assert loaded.created_at.utcoffset() == timedelta(0)
    assert loaded.created_at.microsecond == 123456
    assert type(loaded.status) is ReceiptStatus
    assert type(loaded.items[0].kind) is LineKind
    assert type(loaded.items[0].category) is Category


def test_s1_ac_11_nullable_fields_round_trip_as_none(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11: optional fields come back as None, not as empty strings or zero."""
    original = receipt(
        status=ReceiptStatus.PROCESSING,
        stage=ProcessingStage.QUEUED,
        store_name="",
        store_key="",
        purchased_on=None,
        subtotal=None,
        tax=None,
        total=None,
        payment_last4=None,
        ocr_text=None,
        image_path=None,
        items=(),
    )

    saved = _add(uow_factory, original)
    assert saved.id is not None

    assert _get(uow_factory, saved.id) == saved
    assert _without_ids(saved) == original


def test_s1_ac_11_items_come_back_in_position_order(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11: item order is the receipt's line order, whatever order they were given in."""
    items = tuple(
        line_item(position=position, raw_text=f"LINE {position}") for position in (2, 0, 3, 1)
    )

    saved = _add(uow_factory, receipt(items=items))
    assert saved.id is not None
    loaded = _get(uow_factory, saved.id)

    assert loaded is not None
    assert [item.position for item in loaded.items] == [0, 1, 2, 3]
    assert [item.raw_text for item in loaded.items] == ["LINE 0", "LINE 1", "LINE 2", "LINE 3"]


def test_s1_ac_10_get_missing_returns_none(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-10: get() of an unknown id is None."""
    assert _get(uow_factory, 999) is None


def _add_at(uow_factory: UnitOfWorkFactory, minutes: list[int], **overrides: object) -> list[int]:
    ids = []
    for minute in minutes:
        saved = _add(
            uow_factory, receipt(created_at=at(minute), updated_at=at(minute), **overrides)
        )
        assert saved.id is not None
        ids.append(saved.id)
    return ids


def _list(
    uow_factory: UnitOfWorkFactory,
    page: PageRequest,
    *,
    status: ReceiptStatus | None = None,
    store_key: str | None = None,
) -> tuple[tuple[ReceiptSummary, ...], str | None]:
    with uow_factory() as uow:
        result = uow.receipts.list(page, status=status, store_key=store_key)
    return result.items, result.next_cursor


def test_s1_ac_11_list_is_newest_first_with_id_as_tie_breaker(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """S1-AC-11: created_at desc, then id desc for receipts created in the same instant."""
    first, second, third, same_as_third = _add_at(uow_factory, [0, 10, 20, 20])

    items, cursor = _list(uow_factory, PageRequest())

    assert [summary.id for summary in items] == [same_as_third, third, second, first]
    assert cursor is None


def test_s1_ac_11_list_orders_by_instant_not_by_text(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11: a whole second sorts before a fraction of the same second (time stored as text)."""
    whole = _add(uow_factory, receipt(created_at=at(0)))
    later = _add(uow_factory, receipt(created_at=at(0).replace(microsecond=500)))

    items, _ = _list(uow_factory, PageRequest())

    assert [summary.id for summary in items] == [later.id, whole.id]


def test_s1_ac_11_summary_fields(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11: a summary carries id, status, store, date, total, item count and created_at."""
    saved = _add(uow_factory, receipt())
    assert saved.id is not None

    (summary,), _ = _list(uow_factory, PageRequest())

    assert summary == ReceiptSummary(
        id=saved.id,
        status=ReceiptStatus.COMPLETED,
        store_name=saved.store_name,
        purchased_on=saved.purchased_on,
        total=cad(1635),
        item_count=2,
        created_at=saved.created_at,
    )


def test_s1_ac_11_list_honours_limit_and_cursor(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11: next_cursor only when more rows exist; paging yields each receipt once."""
    ids = _add_at(uow_factory, [0, 1, 2, 3, 4, 5, 5])

    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        items, cursor = _list(uow_factory, PageRequest(limit=3, cursor=cursor))
        pages += 1
        assert len(items) <= 3
        seen += [summary.id for summary in items]
        if cursor is None:
            break

    assert pages == 3
    assert seen == ids[::-1]
    assert len(set(seen)) == len(ids)


def test_s1_ac_11_no_cursor_when_the_page_is_exactly_full(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11: a page holding the last row has no next_cursor."""
    _add_at(uow_factory, [0, 1, 2])

    items, cursor = _list(uow_factory, PageRequest(limit=3))

    assert len(items) == 3
    assert cursor is None


def test_s1_ac_11_list_of_nothing(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11: an empty repository lists an empty page."""
    assert _list(uow_factory, PageRequest()) == ((), None)


def test_s1_ac_11_list_rejects_a_garbage_cursor(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11 / S1-AC-08: a cursor we did not issue is a validation error, not a crash."""
    with pytest.raises(DomainValidationError):
        _list(uow_factory, PageRequest(cursor="garbage"))


def test_s1_ac_11_list_filters_by_status(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11: status filter."""
    (completed,) = _add_at(uow_factory, [0])
    (review,) = _add_at(
        uow_factory, [1], status=ReceiptStatus.NEEDS_REVIEW, stage=ProcessingStage.NEEDS_REVIEW
    )

    items, _ = _list(uow_factory, PageRequest(), status=ReceiptStatus.NEEDS_REVIEW)
    assert [summary.id for summary in items] == [review]
    items, _ = _list(uow_factory, PageRequest(), status=ReceiptStatus.COMPLETED)
    assert [summary.id for summary in items] == [completed]
    items, _ = _list(uow_factory, PageRequest(), status=ReceiptStatus.FAILED)
    assert items == ()


def test_s1_ac_11_list_filters_by_store_key_while_paging(uow_factory: UnitOfWorkFactory) -> None:
    """S1-AC-11: store_key filter, combined with paging and the status filter."""
    costco = _add_at(uow_factory, [0, 2, 4], store_key="costco")
    _add_at(uow_factory, [1, 3], store_key="walmart", store_name="WALMART")

    first, cursor = _list(uow_factory, PageRequest(limit=2), store_key="costco")
    rest, end = _list(uow_factory, PageRequest(limit=2, cursor=cursor), store_key="costco")
    both, _ = _list(uow_factory, PageRequest(), store_key="costco", status=ReceiptStatus.COMPLETED)

    assert [s.id for s in first + rest] == costco[::-1]
    assert end is None
    assert [s.id for s in both] == costco[::-1]


def test_s1_ac_10_update_status_sets_status_stage_and_updated_at(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-10: update_status changes status, stage and updated_at (from the clock) only."""
    saved = _add(
        uow_factory, receipt(status=ReceiptStatus.PROCESSING, stage=ProcessingStage.QUEUED)
    )
    assert saved.id is not None
    receipt_id = saved.id
    fake_clock.advance(timedelta(minutes=3))

    in_unit(
        uow_factory,
        lambda uow: uow.receipts.update_status(
            receipt_id, ReceiptStatus.FAILED, ProcessingStage.FAILED
        ),
    )

    assert _get(uow_factory, receipt_id) == replace(
        saved,
        status=ReceiptStatus.FAILED,
        stage=ProcessingStage.FAILED,
        updated_at=fake_clock.now(),
    )


def test_s1_ac_10_update_status_of_missing_receipt_raises(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """S1-AC-10: update_status raises NotFoundError for an unknown id."""

    def update(uow: UnitOfWork) -> None:
        uow.receipts.update_status(999, ReceiptStatus.FAILED, ProcessingStage.FAILED)

    with pytest.raises(NotFoundError):
        in_unit(uow_factory, update)


def test_s1_ac_10_replace_contents_overwrites_fields_and_items(
    uow_factory: UnitOfWorkFactory, fake_clock: FakeClock
) -> None:
    """S1-AC-10: replace_contents overwrites fields and items; created_at is kept."""
    saved = _add(uow_factory, receipt())
    fake_clock.advance(timedelta(hours=1))
    corrected = replace(
        saved,
        store_name="WALMART",
        store_key="walmart",
        total=cad(999),
        items=(line_item(raw_text="ONLY LINE", line_total=cad(999), unit_price=cad(999)),),
        created_at=at(-100),
    )

    stored = in_unit(uow_factory, lambda uow: uow.receipts.replace_contents(corrected))
    assert saved.id is not None
    loaded = _get(uow_factory, saved.id)

    assert loaded == stored
    assert loaded is not None
    assert loaded.store_name == "WALMART"
    assert [item.raw_text for item in loaded.items] == ["ONLY LINE"]
    assert loaded.items[0].id is not None
    assert loaded.created_at == saved.created_at
    assert loaded.updated_at == fake_clock.now()


def test_s1_ac_10_replace_contents_of_missing_receipt_raises(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """S1-AC-10: replace_contents raises NotFoundError for an unknown id."""
    with pytest.raises(NotFoundError):
        in_unit(uow_factory, lambda uow: uow.receipts.replace_contents(receipt(id=999)))
