"""The Home screen's month: what was spent and saved, by week and by store, and what is
waiting on the person or the worker right now."""

import calendar
from collections.abc import Iterable
from datetime import date, timedelta

from paper_trail.domain.enums import ReceiptStatus
from paper_trail.domain.models import (
    MonthSummary,
    ReceiptSpend,
    StoreSpend,
    WeekSpend,
)
from paper_trail.domain.money import Money, sum_money
from paper_trail.domain.paging import PageRequest
from paper_trail.ports.clock import Clock
from paper_trail.ports.unit_of_work import UnitOfWorkFactory

RECENT_RECEIPTS = 3
WEEK_DAYS = 7
# The 1st-7th, 8th-14th, 15th-21st and the 22nd to the month's end.
WEEKS_PER_MONTH = 4


class GetMonthSummary:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def __call__(self, month: date | None = None) -> MonthSummary:
        """The month of ``month`` (by default the clock's) by purchase date. Read-only."""
        first = (month or self._clock.now().date()).replace(day=1)
        last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
        with self._uow_factory(read_only=True) as uow:
            spends = uow.receipts.spending(first, last)
            open_reviews = uow.reviews.open_count()
            unconfirmed = uow.products.unconfirmed_count()
            processing = uow.receipts.count(ReceiptStatus.PROCESSING)
            recent = uow.receipts.list(PageRequest(limit=RECENT_RECEIPTS)).items
        totals = [(spend, spend.total) for spend in spends if spend.total is not None]
        spent = _sums(total for _, total in totals)
        return MonthSummary(
            month=first,
            spent=spent,
            saved=tuple(
                sum_money(
                    (s.savings for s in spends if s.savings.currency == m.currency), m.currency
                )
                for m in spent
            ),
            trips=len(spends),
            weeks=tuple(
                WeekSpend(start, end, _sums(t for s, t in totals if start <= s.purchased_on <= end))
                for start, end in _weeks(first, last)
            ),
            stores=_by_store(totals),
            open_reviews=open_reviews,
            unconfirmed_products=unconfirmed,
            processing=processing,
            recent=recent,
        )


def _weeks(first: date, last: date) -> list[tuple[date, date]]:
    starts = [first + timedelta(days=WEEK_DAYS * week) for week in range(WEEKS_PER_MONTH)]
    ends = [start - timedelta(days=1) for start in starts[1:]] + [last]
    return list(zip(starts, ends, strict=True))


def _sums(amounts: Iterable[Money]) -> tuple[Money, ...]:
    """One sum per currency, the largest first (then by currency code)."""
    sums: dict[str, Money] = {}
    for amount in amounts:
        sums[amount.currency] = sums.get(amount.currency, Money.zero(amount.currency)) + amount
    return tuple(sorted(sums.values(), key=lambda m: (-m.amount_cents, m.currency)))


def _by_store(totals: Iterable[tuple[ReceiptSpend, Money]]) -> tuple[StoreSpend, ...]:
    """One row per store and currency, the largest first; a store is named as on its latest
    receipt (spends come earliest first)."""
    names: dict[tuple[str, str], str] = {}
    sums: dict[tuple[str, str], Money] = {}
    for spend, total in totals:
        key = (spend.store_key, total.currency)
        names[key] = spend.store_name
        sums[key] = sums.get(key, Money.zero(total.currency)) + total
    rows = [StoreSpend(names[key], amount) for key, amount in sums.items()]
    return tuple(sorted(rows, key=lambda row: (-row.spent.amount_cents, row.store_name)))
