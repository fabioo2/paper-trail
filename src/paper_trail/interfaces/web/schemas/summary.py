"""Body of the month summary shown on Home."""

from datetime import date

from pydantic import BaseModel

from paper_trail.domain.models import MonthSummary, StoreSpend, WeekSpend
from paper_trail.interfaces.web.schemas.receipts import MoneyOut, ReceiptSummaryOut


class WeekSpendOut(BaseModel):
    """``start`` to ``end`` inclusive; ``spent`` one sum per currency, largest first (empty
    when nothing was bought)."""

    start: date
    end: date
    spent: list[MoneyOut]

    @classmethod
    def from_domain(cls, week: WeekSpend) -> "WeekSpendOut":
        return cls(
            start=week.start,
            end=week.end,
            spent=[MoneyOut.from_domain(money) for money in week.spent],
        )


class StoreSpendOut(BaseModel):
    store_name: str  # as printed on the store's latest receipt of the month
    spent: MoneyOut

    @classmethod
    def from_domain(cls, store: StoreSpend) -> "StoreSpendOut":
        return cls(store_name=store.store_name, spent=MoneyOut.from_domain(store.spent))


class MonthSummaryOut(BaseModel):
    """A month's completed receipts by purchase date: ``spent`` and ``saved`` one sum per
    currency (``saved`` in ``spent``'s order), ``trips`` the receipts, ``weeks`` the 1st-7th,
    8th-14th, 15th-21st and 22nd-end, ``stores`` one row per store and currency, largest first.
    ``open_reviews``, ``unconfirmed_products``, ``processing`` and ``recent`` (the newest
    receipts of any month and status) describe the app now, whatever the month."""

    month: str  # YYYY-MM
    spent: list[MoneyOut]
    saved: list[MoneyOut]
    trips: int
    weeks: list[WeekSpendOut]
    stores: list[StoreSpendOut]
    open_reviews: int
    unconfirmed_products: int
    processing: int
    recent: list[ReceiptSummaryOut]

    @classmethod
    def from_domain(cls, summary: MonthSummary) -> "MonthSummaryOut":
        return cls(
            month=summary.month.strftime("%Y-%m"),
            spent=[MoneyOut.from_domain(money) for money in summary.spent],
            saved=[MoneyOut.from_domain(money) for money in summary.saved],
            trips=summary.trips,
            weeks=[WeekSpendOut.from_domain(week) for week in summary.weeks],
            stores=[StoreSpendOut.from_domain(store) for store in summary.stores],
            open_reviews=summary.open_reviews,
            unconfirmed_products=summary.unconfirmed_products,
            processing=summary.processing,
            recent=[ReceiptSummaryOut.from_domain(receipt) for receipt in summary.recent],
        )
