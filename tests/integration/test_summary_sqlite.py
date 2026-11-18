"""Home's month summary on a real database file: ``GetMonthSummary`` over
``ReceiptRepository.spending`` and ``count``, the review queue and the product summaries.

Pinned: only COMPLETED receipts dated inside the month count (the 1st and the last day
included, a neighbouring month's and an undated one not); totals, savings, weeks and stores
are one sum per currency, largest first; weeks are the 1st-7th, 8th-14th, 15th-21st and the
22nd to the month's end; stores group by store key and take the name of their latest receipt;
the waiting counts and the newest receipts describe the app whatever the month.
"""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.application.summary import GetMonthSummary
from paper_trail.domain.enums import (
    FieldSource,
    LineKind,
    MatchSource,
    ReceiptStatus,
    ReviewReason,
)
from paper_trail.domain.models import (
    CONFIRMING_SOURCES,
    MonthSummary,
    Product,
    Receipt,
    StoreSpend,
    WeekSpend,
    is_confirmed,
)
from paper_trail.domain.money import Money
from paper_trail.domain.rules.normalization import normalize_store_name
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import cad, discount_line, item_line, line_item, product, receipt
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, seed_purchase, sqlite_uow_factory
from tests.support import CopyMigrated, require_id

COSTCO = "COSTCO WHOLESALE #1234"
METRO = "METRO #57"


@pytest.fixture
def engine(tmp_path: Path, copy_migrated: CopyMigrated) -> Iterator[Engine]:
    engine = create_engine(copy_migrated(tmp_path / "summary.db"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 18, 12, 0, tzinfo=UTC))


@pytest.fixture
def factory(engine: Engine, clock: FakeClock) -> UnitOfWorkFactory:
    return sqlite_uow_factory(engine, clock)


def _trip(  # noqa: PLR0913 - a test builder: one receipt of one item and its optional discount
    factory: UnitOfWorkFactory,
    store_name: str,
    purchased_on: date | None,
    total: int,
    *,
    saved: int = 0,
    currency: str = "CAD",
    status: ReceiptStatus = ReceiptStatus.COMPLETED,
) -> Receipt:
    lines = [line_item(unit_price=None, line_total=Money(total + saved, currency))]
    if saved:
        lines.append(
            line_item(
                position=1,
                kind=LineKind.DISCOUNT,
                unit_price=None,
                line_total=Money(-saved, currency),
                applies_to_position=0,
            )
        )
    return in_unit(
        factory,
        lambda uow: uow.receipts.add(
            receipt(
                status=status,
                store_name=store_name,
                store_key=normalize_store_name(store_name),
                purchased_on=purchased_on,
                currency=currency,
                subtotal=None,
                tax=None,
                total=Money(total, currency),
                items=tuple(lines),
            )
        ),
    )


def test_a_month_sums_its_completed_receipts_by_week_store_and_currency(
    factory: UnitOfWorkFactory, clock: FakeClock
) -> None:
    _trip(factory, COSTCO, date(2026, 11, 1), 20000, saved=1500)
    _trip(factory, METRO, date(2026, 11, 7), 4204)
    _trip(factory, "Costco", date(2026, 11, 8), 6800, saved=400)
    _trip(factory, METRO, date(2026, 11, 21), 1000)
    _trip(factory, METRO, date(2026, 11, 22), 500)
    _trip(factory, "WHOLE FOODS", date(2026, 11, 30), 2500, currency="USD", saved=100)
    _trip(factory, METRO, date(2026, 10, 31), 99999)
    _trip(factory, METRO, date(2026, 12, 1), 99999)
    _trip(factory, METRO, None, 99999)
    for status in (ReceiptStatus.NEEDS_REVIEW, ReceiptStatus.FAILED, ReceiptStatus.PROCESSING):
        _trip(factory, METRO, date(2026, 11, 15), 99999, status=status)

    summary = GetMonthSummary(factory, clock)(date(2026, 11, 18))

    assert replace(summary, recent=()) == MonthSummary(
        month=date(2026, 11, 1),
        spent=(cad(32504), Money(2500, "USD")),
        saved=(cad(1900), Money(100, "USD")),
        trips=6,
        weeks=(
            WeekSpend(date(2026, 11, 1), date(2026, 11, 7), (cad(24204),)),
            WeekSpend(date(2026, 11, 8), date(2026, 11, 14), (cad(6800),)),
            WeekSpend(date(2026, 11, 15), date(2026, 11, 21), (cad(1000),)),
            WeekSpend(date(2026, 11, 22), date(2026, 11, 30), (Money(2500, "USD"), cad(500))),
        ),
        stores=(
            StoreSpend("Costco", cad(26800)),
            StoreSpend(METRO, cad(5704)),
            StoreSpend("WHOLE FOODS", Money(2500, "USD")),
        ),
        open_reviews=0,
        unconfirmed_products=0,
        processing=1,
        recent=(),
    )


def test_an_empty_february_has_four_empty_weeks_ending_on_the_28th(
    factory: UnitOfWorkFactory, clock: FakeClock
) -> None:
    summary = GetMonthSummary(factory, clock)(date(2027, 2, 1))

    assert (summary.spent, summary.saved, summary.trips, summary.stores) == ((), (), 0, ())
    assert [(week.start.day, week.end.day, week.spent) for week in summary.weeks] == [
        (1, 7, ()),
        (8, 14, ()),
        (15, 21, ()),
        (22, 28, ()),
    ]


def test_the_month_defaults_to_the_clocks_and_counts_what_is_waiting(
    factory: UnitOfWorkFactory, clock: FakeClock
) -> None:
    guessed = in_unit(
        factory,
        lambda uow: uow.products.add(product(field_sources={"canonical_name": FieldSource.LLM})),
    )
    named = in_unit(
        factory,
        lambda uow: uow.products.add(product(field_sources={"canonical_name": FieldSource.PERSON})),
    )
    in_unit(  # never bought: not on the Products page, so not counted
        factory,
        lambda uow: uow.products.add(product(field_sources={"canonical_name": FieldSource.LLM})),
    )
    bought = seed_purchase(
        factory,
        [
            item_line(0, "EGGS", 899, product_id=guessed.id, match_source=MatchSource.NEW),
            item_line(1, "MILK", 599, product_id=named.id, match_source=MatchSource.NEW),
        ],
        purchased_on=date(2026, 11, 2),
    )
    review = _trip(factory, METRO, date(2026, 11, 3), 100, status=ReceiptStatus.NEEDS_REVIEW)
    in_unit(
        factory,
        lambda uow: uow.reviews.open(require_id(review.id), ReviewReason.LOW_CONFIDENCE, {}, None),
    )
    for _ in range(2):
        _trip(factory, "", None, 0, status=ReceiptStatus.PROCESSING)
    newest = _trip(factory, METRO, date(2026, 11, 9), 100)

    summary = GetMonthSummary(factory, clock)()

    assert summary.month == date(2026, 11, 1)
    assert (summary.open_reviews, summary.unconfirmed_products, summary.processing) == (1, 1, 2)
    assert [r.id for r in summary.recent] == [newest.id, review.id, bought.id]


def test_the_unconfirmed_count_agrees_with_the_products_page(factory: UnitOfWorkFactory) -> None:
    sources: list[dict[str, FieldSource]] = [{"canonical_name": s} for s in FieldSource]
    sources += [{}, {"brand": FieldSource.PERSON}]

    def add(field_sources: dict[str, FieldSource]) -> Product:
        return in_unit(factory, lambda uow: uow.products.add(product(field_sources=field_sources)))

    stored = [add(fs) for fs in sources for _ in range(2)]
    bought = stored[::2]
    seed_purchase(
        factory,
        [
            item_line(i, "LINE", 100, product_id=p.id, match_source=MatchSource.NEW)
            for i, p in enumerate(bought)
        ]
        + [discount_line(len(bought), "TPD", -10, None, product_id=stored[1].id)],
    )

    with factory(read_only=True) as uow:
        counted = uow.products.unconfirmed_count()
        listed = sum(not is_confirmed(s.product) for s in uow.products.summaries())

    assert counted == listed == len(sources) - len(CONFIRMING_SOURCES)
