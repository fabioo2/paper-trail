"""The products list on a real database file: ``ListProducts`` over
``ProductRepository.summaries`` (one query, no per-product reads), and one product's purchases
(``ListProductPurchases`` over ``ProductRepository.purchases``).

Pinned: only products with an ITEM line are listed (a DISCOUNT line naming the product is no
purchase), by name ignoring case; ``times_bought`` agrees with the stored ITEM lines; the last
purchase is the newest dated line (the higher receipt id on the same day), paid after its
linked discount, and an undated line only when no line is dated. The recent price points are
the newest dated lines in that order, at most ``RECENT_PRICE_POINTS``, with quantity and unit.
A product's purchases are its ITEM lines in that same order, as many as ``times_bought``, each
with its unit. A line without a price observation (an undated receipt) is paid after the
DISCOUNT line linked to it on its own receipt, in both views (hardening H3-08, H3-09).
"""

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, event

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.application.products import ListProductPurchases, ListProducts
from paper_trail.domain.enums import MatchSource, Unit
from paper_trail.domain.errors import NotFoundError
from paper_trail.domain.models import (
    RECENT_PRICE_POINTS,
    LastPurchase,
    LineItem,
    PricePoint,
    Product,
    ProductPurchase,
    ProductSummary,
    Receipt,
)
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import STORE_NAME, cad, discount_line, item_line, product
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, seed_purchase, sqlite_uow_factory
from tests.support import CopyMigrated, require_id

COSTCO = "COSTCO WHOLESALE #1234"
LOBLAWS = "LOBLAWS #1012"


@pytest.fixture
def engine(tmp_path: Path, copy_migrated: CopyMigrated) -> Iterator[Engine]:
    engine = create_engine(copy_migrated(tmp_path / "products.db"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def factory(engine: Engine) -> UnitOfWorkFactory:
    return sqlite_uow_factory(engine, FakeClock())


def _add(factory: UnitOfWorkFactory, name: str) -> Product:
    return in_unit(factory, lambda uow: uow.products.add(product(canonical_name=name)))


def _bought(  # noqa: PLR0913 - a test builder: one ITEM line and its optional discount
    factory: UnitOfWorkFactory,
    item: Product,
    store_name: str,
    purchased_on: date | None,
    *,
    line_total: int,
    discount: int = 0,
) -> Receipt:
    pid = require_id(item.id)
    lines = [item_line(0, "LINE", line_total, product_id=pid, match_source=MatchSource.CATALOG)]
    if discount:
        lines.append(discount_line(1, "TPD", -discount, 0, product_id=pid))
    return seed_purchase(factory, lines, store_name=store_name, purchased_on=purchased_on)


def test_lists_bought_products_with_their_newest_purchase(
    engine: Engine, factory: UnitOfWorkFactory
) -> None:
    eggs = _add(factory, "Eggs")
    juice = _add(factory, "apple juice")
    bananas = _add(factory, "Bananas")
    _add(factory, "Zucchini")
    _bought(factory, eggs, COSTCO, date(2026, 11, 1), line_total=899)
    _bought(factory, eggs, COSTCO, date(2026, 11, 3), line_total=999)
    _bought(factory, eggs, LOBLAWS, date(2026, 11, 3), line_total=1699, discount=400)
    _bought(factory, eggs, LOBLAWS, None, line_total=1)
    _bought(factory, juice, COSTCO, date(2026, 10, 30), line_total=450)
    _bought(factory, bananas, LOBLAWS, None, line_total=204)
    statements: list[str] = []
    event.listen(engine, "before_cursor_execute", lambda *args: statements.append(args[2]))

    listed = ListProducts(factory)()

    one = Decimal(1)
    assert listed == (
        ProductSummary(
            juice,
            1,
            LastPurchase(date(2026, 10, 30), COSTCO, cad(450)),
            (PricePoint(date(2026, 10, 30), COSTCO, one, cad(450)),),
        ),
        ProductSummary(bananas, 1, LastPurchase(None, LOBLAWS, cad(204))),
        ProductSummary(
            eggs,
            4,
            LastPurchase(date(2026, 11, 3), LOBLAWS, cad(1299)),
            (
                PricePoint(date(2026, 11, 3), LOBLAWS, one, cad(1299)),
                PricePoint(date(2026, 11, 3), COSTCO, one, cad(999)),
                PricePoint(date(2026, 11, 1), COSTCO, one, cad(899)),
            ),
        ),
    )
    assert sum("FROM products" in statement for statement in statements) == 1


def test_recent_price_points_are_the_newest_dated_lines_capped_with_quantity_and_unit(
    factory: UnitOfWorkFactory,
) -> None:
    apples = _add(factory, "Apples")
    pid = require_id(apples.id)
    _bought(factory, apples, LOBLAWS, None, line_total=1)
    days = [date(2026, 1, day) for day in range(1, RECENT_PRICE_POINTS + 3)]
    for day in days:
        _bought(factory, apples, COSTCO, day, line_total=100 + day.day)
    seed_purchase(
        factory,
        [
            item_line(
                0,
                "APPLES",
                452,
                quantity=Decimal("0.452"),
                unit=Unit.KG,
                product_id=pid,
                match_source=MatchSource.CATALOG,
            )
        ],
        store_name=LOBLAWS,
        purchased_on=date(2026, 2, 1),
    )

    (summary,) = ListProducts(factory)()

    assert summary.times_bought == len(days) + 2
    assert summary.recent == (
        PricePoint(date(2026, 2, 1), LOBLAWS, Decimal("0.452"), cad(452), Unit.KG),
        *(
            PricePoint(day, COSTCO, Decimal(1), cad(100 + day.day))
            for day in reversed(days[-(RECENT_PRICE_POINTS - 1) :])
        ),
    )


def test_a_discount_line_alone_is_no_purchase(factory: UnitOfWorkFactory) -> None:
    coupon = _add(factory, "Coupon")
    seed_purchase(
        factory,
        [
            item_line(0, "LINE", 500),
            discount_line(1, "TPD", -100, 0, product_id=require_id(coupon.id)),
        ],
    )

    assert ListProducts(factory)() == ()


def test_a_products_purchases_are_its_item_lines_newest_first(factory: UnitOfWorkFactory) -> None:
    nov_3 = date(2026, 11, 3)
    eggs = _add(factory, "Eggs")
    juice = _add(factory, "Apple juice")
    first = _bought(factory, eggs, COSTCO, date(2026, 11, 1), line_total=899)
    pair = seed_purchase(
        factory,
        [
            item_line(
                0,
                "EGGS",
                1218,
                quantity=Decimal(2),
                unit_price=cad(609),
                product_id=require_id(eggs.id),
                match_source=MatchSource.CATALOG,
            )
        ],
        store_name=COSTCO,
        purchased_on=nov_3,
    )
    _bought(factory, juice, COSTCO, nov_3, line_total=450)
    sale = _bought(factory, eggs, LOBLAWS, nov_3, line_total=1699, discount=400)
    undated = _bought(factory, eggs, LOBLAWS, None, line_total=1)

    purchases = ListProductPurchases(factory)(require_id(eggs.id))

    assert purchases == (
        ProductPurchase(
            require_id(sale.id), nov_3, LOBLAWS, Decimal(1), cad(1699), cad(-400), cad(1299)
        ),
        ProductPurchase(
            require_id(pair.id), nov_3, COSTCO, Decimal(2), cad(609), cad(0), cad(1218)
        ),
        ProductPurchase(
            require_id(first.id), date(2026, 11, 1), COSTCO, Decimal(1), cad(899), cad(0), cad(899)
        ),
        ProductPurchase(require_id(undated.id), None, LOBLAWS, Decimal(1), cad(1), cad(0), cad(1)),
    )
    summary = next(s for s in ListProducts(factory)() if s.product == eggs)
    assert summary.times_bought == len(purchases)


def test_purchases_of_a_product_never_bought_and_of_an_unknown_one(
    factory: UnitOfWorkFactory,
) -> None:
    unbought = _add(factory, "Zucchini")

    assert ListProductPurchases(factory)(require_id(unbought.id)) == ()
    with pytest.raises(NotFoundError, match="product 424242 does not exist"):
        ListProductPurchases(factory)(424242)


def test_two_lines_of_one_receipt_are_listed_by_position_descending(
    factory: UnitOfWorkFactory,
) -> None:
    eggs = _add(factory, "Eggs")
    pid = require_id(eggs.id)
    receipt = seed_purchase(
        factory,
        [
            item_line(0, "EGGS", 899, product_id=pid, match_source=MatchSource.CATALOG),
            item_line(1, "EGGS", 999, product_id=pid, match_source=MatchSource.CATALOG),
        ],
    )

    purchases = ListProductPurchases(factory)(pid)

    assert [(p.receipt_id, p.paid) for p in purchases] == [
        (receipt.id, cad(999)),
        (receipt.id, cad(899)),
    ]


# --- H3-08, H3-09: an undated purchase is paid after its linked discount ---------------------


def _undated(
    factory: UnitOfWorkFactory, pid: int, *extra: LineItem, unit: Unit | None = None
) -> Receipt:
    lines = [item_line(0, "EGGS", 899, product_id=pid, match_source=MatchSource.CATALOG, unit=unit)]
    return seed_purchase(factory, [*lines, *extra], purchased_on=None)


@pytest.mark.parametrize(
    ("extra", "discount", "paid"),
    [
        ((), 0, 899),
        ((discount_line(1, "TPD", -150, 0),), -150, 749),
        ((discount_line(1, "MEMBER SAVINGS", -150),), 0, 899),
    ],
    ids=["no-discount", "linked-discount", "receipt-level-discount"],
)
def test_h3_08_an_undated_purchase_is_paid_after_its_linked_discount(
    factory: UnitOfWorkFactory, extra: tuple[LineItem, ...], discount: int, paid: int
) -> None:
    eggs = _add(factory, "Eggs")
    pid = require_id(eggs.id)
    undated = _undated(factory, pid, *extra, unit=Unit.KG)

    purchases = ListProductPurchases(factory)(pid)
    summaries = ListProducts(factory)()

    assert purchases == (
        ProductPurchase(
            require_id(undated.id),
            None,
            STORE_NAME,
            Decimal(1),
            cad(899),
            cad(discount),
            cad(paid),
            Unit.KG,
        ),
    )
    assert summaries == (ProductSummary(eggs, 1, LastPurchase(None, STORE_NAME, cad(paid)), ()),)


def test_h3_08_a_discount_at_the_same_position_on_another_receipt_is_not_applied(
    factory: UnitOfWorkFactory,
) -> None:
    eggs = _add(factory, "Eggs")
    pid = require_id(eggs.id)
    undated = _undated(factory, pid)
    seed_purchase(
        factory,
        [item_line(0, "MILK", 500), discount_line(1, "TPD", -200, 0)],
        purchased_on=None,
    )

    purchases = ListProductPurchases(factory)(pid)

    assert [(p.receipt_id, p.discount, p.paid) for p in purchases] == [
        (undated.id, cad(0), cad(899))
    ]


def test_h3_09_both_views_agree_on_what_was_paid_dated_and_undated(
    factory: UnitOfWorkFactory,
) -> None:
    dated, undated = _add(factory, "Dated eggs"), _add(factory, "Undated eggs")
    _bought(factory, dated, COSTCO, date(2026, 11, 3), line_total=1699, discount=400)
    _undated(factory, require_id(undated.id), discount_line(1, "TPD", -150, 0))
    _undated(factory, require_id(undated.id), discount_line(1, "TPD", -100, 0))

    for summary in ListProducts(factory)():
        purchases = ListProductPurchases(factory)(require_id(summary.product.id))
        assert summary.times_bought == len(purchases)
        assert summary.last_purchase.paid == purchases[0].paid
