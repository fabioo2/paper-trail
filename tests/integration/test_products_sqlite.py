"""The products list on a real database file: ``ListProducts`` over
``ProductRepository.summaries`` (one query, no per-product reads).

Pinned: only products with an ITEM line are listed (a DISCOUNT line naming the product is no
purchase), by name ignoring case; ``times_bought`` agrees with the stored ITEM lines; the last
purchase is the newest dated line (the higher receipt id on the same day), paid after its
linked discount, and an undated line (its line total) only when no line is dated.
"""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine, event

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.application.products import ListProducts
from paper_trail.domain.enums import MatchSource
from paper_trail.domain.models import LastPurchase, Product, ProductSummary
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import cad, discount_line, item_line, product
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
) -> None:
    pid = require_id(item.id)
    lines = [item_line(0, "LINE", line_total, product_id=pid, match_source=MatchSource.CATALOG)]
    if discount:
        lines.append(discount_line(1, "TPD", -discount, 0, product_id=pid))
    seed_purchase(factory, lines, store_name=store_name, purchased_on=purchased_on)


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

    assert listed == (
        ProductSummary(juice, 1, LastPurchase(date(2026, 10, 30), COSTCO, cad(450))),
        ProductSummary(bananas, 1, LastPurchase(None, LOBLAWS, cad(204))),
        ProductSummary(eggs, 4, LastPurchase(date(2026, 11, 3), LOBLAWS, cad(1299))),
    )
    assert sum("FROM products" in statement for statement in statements) == 1


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
