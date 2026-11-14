"""``BackfillPrices``: discounts of receipts stored before they were linked (R9, A4-A6).

Pinned (docs/plans/discounts-and-paid-prices.md, plan answers Q11, Q20, Q22-Q28):
- ``BackfillPrices(uow_factory)(*, receipt_id=None, apply=False) -> PriceBackfill``; COMPLETED
  receipts only, newest ``created_at`` first, then by position; one ``BackfilledDiscount`` per
  DISCOUNT line with the stored discount line, the stored item it reduces (None at receipt
  level), the reason it stays at receipt level, whether its link is new and whether the item's
  observation takes (dry run: would take) the discount.
- A dry run writes nothing. ``apply`` sets the new links and the discount of each linked item's
  observation; an existing link is kept; an undated receipt or an unmatched item gets the link
  only; nothing is ever created. A second ``apply`` changes nothing.
- NotFoundError "unknown receipt" for an unknown ``receipt_id``, including 0.

The receipts here are stored the way migration 0004 leaves existing data: no links, every
observation at discount 0.
"""

import copy
from collections.abc import Sequence
from dataclasses import replace
from datetime import date

import pytest

from paper_trail.application.prices import BackfilledDiscount, BackfillPrices, PriceBackfill
from paper_trail.domain.enums import ProcessingStage, ReceiptStatus
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import PriceObservation, Receipt
from paper_trail.domain.money import Money
from paper_trail.domain.paging import MAX_PAGE_LIMIT
from paper_trail.domain.rules.discounts import Refusal
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import (
    CHOC,
    PRINTED_LINKS,
    PRINTED_MULTIPLE,
    PRINTED_SUBTOTAL,
    PRINTED_TAX,
    PRINTED_TOTAL,
    at,
    cad,
    discount_line,
    item_line,
    printed_lines,
)
from tests.fakes.clock import FakeClock
from tests.fakes.state import FakeState
from tests.fakes.unit_of_work import FakeDatabase, fake_uow_factory
from tests.storage import seed_pre_feature
from tests.support import require_id

type Outcome = tuple[int, int, int | None, Refusal | None, bool, bool]


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
def backfill(uow_factory: UnitOfWorkFactory) -> BackfillPrices:
    return BackfillPrices(uow_factory)


def _printed(uow_factory: UnitOfWorkFactory) -> Receipt:
    """PRINTED, stored first (id 1) at 12:10, bought 11-01."""
    return seed_pre_feature(
        uow_factory,
        printed_lines(),
        created_at=at(10),
        totals=(PRINTED_SUBTOTAL, PRINTED_TAX, PRINTED_TOTAL),
        purchased_on=date(2026, 11, 1),
    )


def _simple(uow_factory: UnitOfWorkFactory, **overrides: object) -> Receipt:
    """SIMPLE, stored second (id 2) at 12:00, bought 11-05: older, but bought later."""
    values: dict[str, object] = {"created_at": at(0), "purchased_on": date(2026, 11, 5)}
    values.update(overrides)
    return seed_pre_feature(uow_factory, **values)  # type: ignore[arg-type]


def _both(uow_factory: UnitOfWorkFactory) -> tuple[Receipt, Receipt]:
    return _printed(uow_factory), _simple(uow_factory)


def _entry(  # noqa: PLR0913 - a test builder: keyword-only flags with defaults
    stored: Receipt,
    position: int,
    item: int | None,
    reason: Refusal | None = None,
    *,
    new_link: bool = True,
    price_updated: bool = True,
) -> BackfilledDiscount:
    return BackfilledDiscount(
        receipt_id=require_id(stored.id),
        discount=stored.items[position],
        item=None if item is None else stored.items[item],
        reason=reason,
        new_link=new_link,
        price_updated=price_updated,
    )


def _printed_entries(stored: Receipt, *, fresh: bool = True) -> list[BackfilledDiscount]:
    """PRINTED's eight discounts in position order: the MULTIPLE, then the seven links."""
    multiple = _entry(
        stored, PRINTED_MULTIPLE, None, Refusal.MULTIPLE, new_link=False, price_updated=False
    )
    links = [
        _entry(stored, position, item, new_link=fresh, price_updated=fresh)
        for position, item in PRINTED_LINKS.items()
    ]
    return [multiple, *links]


def _outcomes(found: PriceBackfill) -> list[Outcome]:
    return [
        (
            entry.receipt_id,
            entry.discount.position,
            None if entry.item is None else entry.item.position,
            entry.reason,
            entry.new_link,
            entry.price_updated,
        )
        for entry in found.discounts
    ]


def _project(entries: Sequence[BackfilledDiscount]) -> list[Outcome]:
    return _outcomes(PriceBackfill(tuple(entries), applied=False))


def _links(state: FakeState) -> dict[tuple[int, int], int]:
    """Every stored link: (receipt id, discount position) -> item position."""
    return {
        (receipt_id, item.position): item.applies_to_position
        for receipt_id, stored in state.receipts.items()
        for item in stored.items
        if item.applies_to_position is not None
    }


def _by_line(state: FakeState) -> dict[tuple[int, int], PriceObservation]:
    """Every observation by (receipt id, line position)."""
    positions = {
        item.id: (receipt_id, item.position)
        for receipt_id, stored in state.receipts.items()
        for item in stored.items
    }
    return {positions[row.line_item_id]: row for row in state.prices.values()}


def _discounts(state: FakeState) -> dict[tuple[int, int], tuple[Money, Money, bool]]:
    """Every observation: (receipt id, line position) -> (discount, paid, on_sale)."""
    return {key: (row.discount, row.paid, row.on_sale) for key, row in _by_line(state).items()}


def _regular(row: PriceObservation) -> PriceObservation:
    return replace(row, discount=Money.zero(row.line_total.currency))


def _unlinked(stored: Receipt, before: Receipt) -> Receipt:
    items = tuple(replace(item, applies_to_position=None) for item in stored.items)
    return replace(stored, items=items, updated_at=before.updated_at)


# --- dry run and apply (TC-64, TC-65, TC-66) ------------------------------------------------------


def test_tc_64_a_dry_run_lists_every_discount_newest_receipt_first_and_writes_nothing(
    backfill: BackfillPrices, uow_factory: UnitOfWorkFactory, database: FakeDatabase
) -> None:
    """TC-64 (R9, Q11, Q20): PRINTED (id 1, 12:10, bought 11-01) before SIMPLE (id 2, 12:00,
    bought 11-05): by created_at, neither by id nor by purchase date."""
    printed, simple = _both(uow_factory)
    before, commits = copy.deepcopy(database.state), database.commits

    found = backfill()

    assert found == PriceBackfill(
        discounts=(*_printed_entries(printed), _entry(simple, 4, 0)), applied=False
    )
    assert (found.links_set, found.prices_updated, found.changes) == (8, 8, 16)
    assert database.commits == commits
    assert database.state == before


def test_tc_65_apply_sets_the_links_and_the_observations_discounts(
    backfill: BackfillPrices, uow_factory: UnitOfWorkFactory, database: FakeDatabase
) -> None:
    """TC-65 (R9): eight links, eight observations; nothing else changes and the counts are the
    stored changes."""
    printed, simple = _both(uow_factory)
    a, b = require_id(printed.id), require_id(simple.id)
    before = copy.deepcopy(database.state)

    found = backfill(apply=True)

    assert found.applied
    assert _outcomes(found) == _project([*_printed_entries(printed), _entry(simple, 4, 0)])
    assert (found.links_set, found.prices_updated, found.changes) == (8, 8, 16)
    after = database.state
    assert _links(after) == {
        **{(a, position): item for position, item in PRINTED_LINKS.items()},
        (b, 4): 0,
    }
    expected = {key: (cad(0), row.line_total, False) for key, row in _by_line(before).items()}
    expected.update({(a, item): (cad(-400), cad(1299), True) for item in (5, 7, 9, 11, 13)})
    expected.update({(a, item): (cad(-400), cad(1199), True) for item in (15, 17)})
    expected[b, 0] = (cad(-500), cad(399), True)
    assert _discounts(after) == expected
    assert {key: _regular(row) for key, row in after.prices.items()} == before.prices
    assert {
        receipt_id: _unlinked(stored, before.receipts[receipt_id])
        for receipt_id, stored in after.receipts.items()
    } == before.receipts
    assert (after.products, after.aliases) == (before.products, before.aliases)


def test_tc_66_a_second_apply_changes_nothing_and_still_reports_the_effective_links(
    backfill: BackfillPrices, uow_factory: UnitOfWorkFactory, database: FakeDatabase
) -> None:
    """TC-66 (R9, A4): 0 links set, 0 observations updated; still 8 linked, 1 receipt level."""
    printed, simple = _both(uow_factory)
    backfill(apply=True)
    once = copy.deepcopy(database.state)

    again = backfill(apply=True)

    assert _outcomes(again) == _project(
        [
            *_printed_entries(printed, fresh=False),
            _entry(simple, 4, 0, new_link=False, price_updated=False),
        ]
    )
    assert (again.links_set, again.prices_updated, again.changes) == (0, 0, 0)
    assert database.state == once


# --- links already stored, link only, other statuses (TC-67, TC-68, TC-116, TC-119) --------------


def test_tc_67_a_stored_link_is_kept_and_its_item_gets_the_discount(
    backfill: BackfillPrices, uow_factory: UnitOfWorkFactory, database: FakeDatabase
) -> None:
    """TC-67 (R9, R4): 2 -> 0 is stored where the rule would pick 1; item 0's observation is
    still at discount 0. The link is kept (0 links set) and item 0's observation takes -4.00;
    item 1's stays regular."""
    lines = (
        item_line(0, CHOC, 1699),
        item_line(1, CHOC, 1699),
        discount_line(2, "TPD/1362394", -400, 0),
    )
    stored = seed_pre_feature(uow_factory, lines, created_at=at(0), totals=(2998, 0, 2998))
    receipt_id = require_id(stored.id)

    found = backfill(apply=True)

    assert _outcomes(found) == [(receipt_id, 2, 0, None, False, True)]
    assert (found.links_set, found.prices_updated) == (0, 1)
    assert _links(database.state) == {(receipt_id, 2): 0}
    assert _discounts(database.state) == {
        (receipt_id, 0): (cad(-400), cad(1299), True),
        (receipt_id, 1): (cad(0), cad(1699), False),
    }


@pytest.mark.parametrize(
    ("purchased_on", "unmatched"),
    [(None, frozenset[int]()), (date(2026, 11, 5), frozenset({0}))],
    ids=["tc-68-undated", "tc-116-unmatched-item"],
)
def test_tc_68_116_link_only_when_the_item_has_no_observation(
    backfill: BackfillPrices,
    uow_factory: UnitOfWorkFactory,
    database: FakeDatabase,
    purchased_on: date | None,
    unmatched: frozenset[int],
) -> None:
    """TC-68 (R9) and TC-116 (A5): an undated receipt, or a dated one whose linked item is
    unmatched: the link is set, reported "link only"; no observation is written or created."""
    stored = _simple(uow_factory, purchased_on=purchased_on, unmatched=unmatched)
    receipt_id = require_id(stored.id)
    prices = copy.deepcopy(database.state.prices)

    found = backfill(apply=True)

    assert _outcomes(found) == [(receipt_id, 4, 0, None, True, False)]
    assert (found.links_set, found.prices_updated) == (1, 0)
    assert _links(database.state) == {(receipt_id, 4): 0}
    assert database.state.prices == prices


@pytest.mark.parametrize(
    ("status", "stage"),
    [
        (ReceiptStatus.NEEDS_REVIEW, ProcessingStage.NEEDS_REVIEW),
        (ReceiptStatus.PROCESSING, ProcessingStage.MATCHING),
        (ReceiptStatus.FAILED, ProcessingStage.FAILED),
    ],
    ids=["needs-review", "processing", "failed"],
)
def test_tc_119_only_completed_receipts_are_backfilled(
    uow_factory: UnitOfWorkFactory,
    database: FakeDatabase,
    status: ReceiptStatus,
    stage: ProcessingStage,
) -> None:
    """TC-119 (Q11): a receipt that is not COMPLETED is neither listed nor changed."""
    _simple(uow_factory, status=status, stage=stage)
    before = copy.deepcopy(database.state)

    found = BackfillPrices(uow_factory)(apply=True)

    assert found == PriceBackfill(discounts=(), applied=True)
    assert database.state == before


def test_tc_123_every_completed_receipt_is_backfilled_past_the_first_page(
    backfill: BackfillPrices, uow_factory: UnitOfWorkFactory
) -> None:
    """The scan pages through the COMPLETED receipts: one more than a page, all processed."""
    lines = (item_line(0, CHOC, 1699), discount_line(1, "TPD/1362394", -400))
    stored = [
        seed_pre_feature(uow_factory, lines, created_at=at(minute), totals=(1299, 0, 1299))
        for minute in range(MAX_PAGE_LIMIT + 1)
    ]

    found = backfill(apply=True)

    assert [entry.receipt_id for entry in found.discounts] == [
        require_id(saved.id) for saved in reversed(stored)
    ]
    assert found.links_set == MAX_PAGE_LIMIT + 1


# --- one receipt (TC-70) --------------------------------------------------------------------------


def test_tc_70_one_receipt_only(
    backfill: BackfillPrices, uow_factory: UnitOfWorkFactory, database: FakeDatabase
) -> None:
    """TC-70 (R9, Q11): ``receipt_id`` = SIMPLE: only SIMPLE is listed and changed."""
    printed, simple = _both(uow_factory)
    b = require_id(simple.id)
    before = copy.deepcopy(database.state)

    found = backfill(receipt_id=b, apply=True)

    assert _outcomes(found) == [(b, 4, 0, None, True, True)]
    assert _links(database.state) == {(b, 4): 0}
    a = require_id(printed.id)
    assert database.state.receipts[a] == before.receipts[a]
    assert {key: row for key, row in database.state.prices.items() if row.receipt_id == a} == {
        key: row for key, row in before.prices.items() if row.receipt_id == a
    }


@pytest.mark.parametrize("receipt_id", [999, 0])
def test_tc_70_an_unknown_receipt_is_not_found_and_nothing_is_written(
    backfill: BackfillPrices,
    uow_factory: UnitOfWorkFactory,
    database: FakeDatabase,
    receipt_id: int,
) -> None:
    """TC-70 (Q11): an unknown id, including 0, is NotFoundError "unknown receipt"."""
    _both(uow_factory)
    before, commits = copy.deepcopy(database.state), database.commits

    with pytest.raises(NotFoundError, match="unknown receipt"):
        backfill(receipt_id=receipt_id, apply=True)

    assert (database.state, database.commits) == (before, commits)
