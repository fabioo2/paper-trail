"""GetReceipt and ListReceipts with in-memory fakes (S1-AC-22 read path, PLAN 3.6).

Pinned (docs/tests/stage1.md): ``GetReceipt(uow_factory, tolerances)`` checks with the same
tolerances RecordReceipt was given; ``ListReceipts(uow_factory)``.
"""

from dataclasses import replace

import pytest

from paper_trail.application.receipts import GetReceipt, ListReceipts, RecordReceipt
from paper_trail.domain.enums import ReceiptStatus
from paper_trail.domain.errors import DomainValidationError, NotFoundError
from paper_trail.domain.models import Receipt
from paper_trail.domain.paging import PageRequest
from paper_trail.domain.rules.arithmetic import IssueCode, Tolerances
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import cad, receipt_draft
from tests.fakes.clock import FakeClock
from tests.fakes.unit_of_work import FakeDatabase, fake_uow_factory
from tests.storage import in_unit
from tests.support import require_id


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def uow_factory(clock: FakeClock) -> UnitOfWorkFactory:
    return fake_uow_factory(FakeDatabase(), clock)


@pytest.fixture
def record(uow_factory: UnitOfWorkFactory, clock: FakeClock) -> RecordReceipt:
    return RecordReceipt(uow_factory, clock, Tolerances())


def test_s1_ac_22_get_receipt_returns_the_receipt_and_its_report(
    uow_factory: UnitOfWorkFactory, record: RecordReceipt
) -> None:
    """GetReceipt returns the stored receipt and a freshly computed arithmetic report."""
    saved = record(receipt_draft()).receipt

    loaded, report = GetReceipt(uow_factory, Tolerances())(require_id(saved.id))

    assert loaded == saved
    assert report.ok


def test_s1_ac_22_get_receipt_recomputes_the_report_on_read(
    uow_factory: UnitOfWorkFactory, record: RecordReceipt
) -> None:
    """PLAN 3.6: the report reflects the data as it is now, not as it was recorded."""
    saved = record(receipt_draft()).receipt
    broken = replace(saved, total=cad(9999))
    in_unit(uow_factory, lambda uow: uow.receipts.replace_contents(broken))

    _, report = GetReceipt(uow_factory, Tolerances())(require_id(saved.id))

    assert [issue.code for issue in report.issues] == [IssueCode.SUBTOTAL_PLUS_TAX_VS_TOTAL]


def test_s1_ac_22_get_receipt_uses_the_injected_tolerances(
    uow_factory: UnitOfWorkFactory, clock: FakeClock
) -> None:
    """Pinned: the report on read uses the tolerances of the container, not hard-coded ones."""
    lenient = Tolerances(per_line_cents=1, total_cents=1)
    saved = RecordReceipt(uow_factory, clock, lenient)(receipt_draft(total=cad(1636))).receipt

    _, strict_report = GetReceipt(uow_factory, Tolerances())(require_id(saved.id))
    _, lenient_report = GetReceipt(uow_factory, lenient)(require_id(saved.id))

    assert not strict_report.ok
    assert lenient_report.ok


def test_s1_ac_22_get_unknown_receipt_raises_not_found(uow_factory: UnitOfWorkFactory) -> None:
    """GetReceipt raises NotFoundError (HTTP 404) for an unknown id."""
    with pytest.raises(NotFoundError):
        GetReceipt(uow_factory, Tolerances())(404)


def _record_many(record: RecordReceipt, clock: FakeClock) -> list[Receipt]:
    saved = []
    for store, total in [("COSTCO WHOLESALE #1234", 1635), ("Walmart Supercentre 3150", 1635),
                         ("Costco", 1700), ("COSTCO #99", 1635)]:  # fmt: skip
        saved.append(record(receipt_draft(store_name=store, total=cad(total))).receipt)
        clock.advance(60)
    return saved


def test_s1_ac_22_list_receipts_pages_newest_first(
    uow_factory: UnitOfWorkFactory, record: RecordReceipt, clock: FakeClock
) -> None:
    """ListReceipts pages newest first and hands out a cursor while more remain."""
    saved = _record_many(record, clock)
    list_receipts = ListReceipts(uow_factory)

    first = list_receipts(PageRequest(limit=3), None, None)
    rest = list_receipts(PageRequest(limit=3, cursor=first.next_cursor), None, None)

    assert [s.id for s in first.items + rest.items] == [r.id for r in reversed(saved)]
    assert rest.next_cursor is None


def test_s1_ac_22_list_receipts_normalizes_the_store_filter(
    uow_factory: UnitOfWorkFactory, record: RecordReceipt, clock: FakeClock
) -> None:
    """PLAN 3.6: `store` is normalized with normalize_store_name before querying."""
    saved = _record_many(record, clock)
    costco = [r.id for r in reversed(saved) if r.store_key == "costco"]

    for store in ("costco", "COSTCO WHOLESALE", "Costco #1"):
        page = ListReceipts(uow_factory)(PageRequest(), None, store)
        assert [s.id for s in page.items] == costco, store


def test_s1_ac_22_list_receipts_filters_by_status(
    uow_factory: UnitOfWorkFactory, record: RecordReceipt, clock: FakeClock
) -> None:
    """ListReceipts passes the status filter through, alone and with the store filter."""
    saved = _record_many(record, clock)
    review = [r.id for r in saved if r.status is ReceiptStatus.NEEDS_REVIEW]
    assert len(review) == 1
    list_receipts = ListReceipts(uow_factory)

    assert [
        s.id for s in list_receipts(PageRequest(), ReceiptStatus.NEEDS_REVIEW, None).items
    ] == review
    assert [
        s.id for s in list_receipts(PageRequest(), ReceiptStatus.NEEDS_REVIEW, "walmart").items
    ] == []


def test_s1_ac_22_list_receipts_rejects_a_bad_cursor(uow_factory: UnitOfWorkFactory) -> None:
    """A cursor that was not issued by us is a DomainValidationError (HTTP 422)."""
    with pytest.raises(DomainValidationError):
        ListReceipts(uow_factory)(PageRequest(cursor="not-a-cursor"), None, None)
