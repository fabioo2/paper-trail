"""``RecordReceipt.replace`` (PLAN 5) and two Stage 1 review notes on ``RecordReceipt``.

``replace(receipt_id, draft, *, force=False)`` is what the graph's ``save_receipt`` tool calls:
the same masking, arithmetic and linking as recording, applied to a receipt that already exists.

Pinned (docs/tests/agent-core-A.md):
- one unit of work; ``created_at`` and ``image_sha256`` are kept; ``ocr_text`` and ``image_path``
  are kept when the draft has none; ``updated_at`` is the clock's time;
- status COMPLETED/DONE when the draft adds up or ``force`` is set, else NEEDS_REVIEW/NEEDS_REVIEW;
  ``replace`` never opens a review item (the graph's review node does);
- price observations only when the arithmetic is ok and the date is known (as in Stage 1);
- an unknown receipt id raises NotFoundError.
Review notes: a draft that mixes currencies is recorded NEEDS_REVIEW with CURRENCY_MISMATCH
issues instead of raising, its foreign amounts stored as the same cents in the receipt's
currency (the review keeps the original currency in ``actual``); a zero unit price is a price.
"""

from dataclasses import replace

import pytest

from paper_trail.application.receipts import RecordReceipt
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus, ReviewReason
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import ReceiptDraft
from paper_trail.domain.money import Money
from paper_trail.domain.rules.arithmetic import IssueCode, Tolerances
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import (
    MASKED_TEST_PAN,
    PURCHASED_ON,
    TEST_PAN,
    at,
    cad,
    line_item,
    receipt,
    receipt_draft,
)
from tests.fakes.clock import FakeClock
from tests.fakes.unit_of_work import FakeDatabase, fake_uow_factory
from tests.storage import in_unit
from tests.support import require_id

SHA = "ab" * 32


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def uow_factory(database: FakeDatabase, clock: FakeClock) -> UnitOfWorkFactory:
    return fake_uow_factory(database, clock)


@pytest.fixture
def record(uow_factory: UnitOfWorkFactory, clock: FakeClock) -> RecordReceipt:
    return RecordReceipt(uow_factory, clock, Tolerances())


def _processing(uow_factory: UnitOfWorkFactory) -> int:
    """A receipt as ProcessReceiptFile creates it: PROCESSING/READING, empty, with an image."""
    placeholder = receipt(
        status=ReceiptStatus.PROCESSING,
        stage=ProcessingStage.READING,
        store_name="",
        store_key="",
        purchased_on=None,
        subtotal=None,
        tax=None,
        total=None,
        payment_last4=None,
        ocr_text="COSTCO WHOLESALE #1234\nTOTAL 16.35",
        image_path="ab/ab/" + SHA + ".jpg",
        items=(),
        image_sha256=SHA,
    )
    return require_id(in_unit(uow_factory, lambda uow: uow.receipts.add(placeholder)).id)


def _inconsistent() -> ReceiptDraft:
    eggs, milk = receipt_draft().items
    return receipt_draft(items=(eggs, replace(milk, line_total=cad(685))))


def test_replace_fills_the_existing_receipt(
    record: RecordReceipt, uow_factory: UnitOfWorkFactory, clock: FakeClock, database: FakeDatabase
) -> None:
    receipt_id = _processing(uow_factory)
    clock.set(at(30))
    commits = database.commits

    result = record.replace(receipt_id, receipt_draft(ocr_text=None, image_path=None))

    saved = result.receipt
    assert saved.id == receipt_id
    assert (saved.status, saved.stage) == (ReceiptStatus.COMPLETED, ProcessingStage.DONE)
    assert saved.store_key == "costco"
    assert saved.total == cad(1635)
    assert len(saved.items) == 2
    assert saved.created_at == at(0)
    assert saved.updated_at == at(30)
    assert saved.image_sha256 == SHA
    assert saved.image_path == "ab/ab/" + SHA + ".jpg"
    assert saved.ocr_text == "COSTCO WHOLESALE #1234\nTOTAL 16.35"
    assert result.review is None
    assert database.commits == commits + 1


def test_replace_masks_like_recording(
    record: RecordReceipt, uow_factory: UnitOfWorkFactory
) -> None:
    receipt_id = _processing(uow_factory)
    first, second = receipt_draft().items
    draft = receipt_draft(
        ocr_text=f"VISA {TEST_PAN}", items=(replace(first, raw_text=f"EGGS {TEST_PAN}"), second)
    )

    result = record.replace(receipt_id, draft)

    assert result.masked_pans == 2
    assert result.receipt.ocr_text == f"VISA {MASKED_TEST_PAN}"
    assert result.receipt.items[0].raw_text == f"EGGS {MASKED_TEST_PAN}"


def test_replace_with_an_inconsistent_draft_needs_review_but_opens_nothing(
    record: RecordReceipt, uow_factory: UnitOfWorkFactory
) -> None:
    receipt_id = _processing(uow_factory)

    result = record.replace(receipt_id, _inconsistent())

    assert result.receipt.status is ReceiptStatus.NEEDS_REVIEW
    assert result.receipt.stage is ProcessingStage.NEEDS_REVIEW
    assert not result.arithmetic.ok
    assert result.review is None
    with uow_factory(read_only=True) as uow:
        assert uow.reviews.open_count() == 0


def test_replace_with_force_completes_an_inconsistent_draft_without_prices(
    record: RecordReceipt, uow_factory: UnitOfWorkFactory, database: FakeDatabase
) -> None:
    receipt_id = _processing(uow_factory)
    draft = replace(_inconsistent(), product_names={0: "Eggs"})

    result = record.replace(receipt_id, draft, force=True)

    assert result.receipt.status is ReceiptStatus.COMPLETED
    assert not result.arithmetic.ok
    assert result.receipt.items[0].product_id is not None
    assert database.state.prices == {}


def test_replace_prices_linked_item_lines_when_consistent(
    record: RecordReceipt, uow_factory: UnitOfWorkFactory, database: FakeDatabase
) -> None:
    receipt_id = _processing(uow_factory)
    draft = replace(receipt_draft(), product_names={0: "Eggs", 1: "Milk"})

    record.replace(receipt_id, draft)

    observations = sorted(database.state.prices.values(), key=lambda row: row.line_item_id)
    assert [row.observed_on for row in observations] == [PURCHASED_ON, PURCHASED_ON]
    assert {row.receipt_id for row in observations} == {receipt_id}


def test_replace_of_an_unknown_receipt_is_not_found(record: RecordReceipt) -> None:
    with pytest.raises(NotFoundError):
        record.replace(404, receipt_draft())


# --- Stage 1 review notes ------------------------------------------------------------------------


def test_review_note_mixed_currencies_go_to_review_instead_of_failing(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """ReceiptDraft's docstring: a draft may mix currencies; check_arithmetic reports it."""
    eggs, milk = receipt_draft().items
    draft = receipt_draft(items=(eggs, replace(milk, line_total=Money(658, "USD"))))

    result = record(draft)

    assert result.receipt.status is ReceiptStatus.NEEDS_REVIEW
    assert result.review is not None
    assert result.review.reason is ReviewReason.VALIDATION_FAILED
    assert {issue.code for issue in result.arithmetic.issues} == {IssueCode.CURRENCY_MISMATCH}
    assert result.review.details["issues"] == [
        {
            "code": "currency_mismatch",
            "message": result.arithmetic.issues[0].message,
            "expected": None,
            "actual": {"amount_cents": 658, "currency": "USD"},
            "position": 1,
        }
    ]
    assert result.receipt.items[1].line_total == Money(658, "CAD")
    assert database.state.prices == {}


def test_review_note_a_zero_unit_price_is_a_price(
    record: RecordReceipt, database: FakeDatabase
) -> None:
    """``_observations`` uses an explicit None check: a free item's unit price is 0.00."""
    free = line_item(position=2, raw_text="FREE BAG", unit_price=cad(0), line_total=cad(0))
    draft = replace(
        receipt_draft(items=(*receipt_draft().items, free)), product_names={2: "Reusable Bag"}
    )

    record(draft)

    (observation,) = database.state.prices.values()
    assert observation.unit_price == cad(0)
