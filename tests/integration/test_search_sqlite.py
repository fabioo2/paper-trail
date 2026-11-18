"""The search across products and receipts on a real database file: ``SearchPurchases`` over
the FTS5 index, ``ProductRepository.summaries`` and ``ReceiptRepository.matching``.

Pinned: products are the ones bought, best text match (name, brand, tag) first, then those of a
matching category by name, at most 10; receipts match by store name (a substring, ``%`` and
``_`` plain text), by a line's printed text or description, or by a line's product's name, newest
purchase first, at most 20, each with its matching ITEM lines' descriptions by position.
"""

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.application.search import SearchPurchases
from paper_trail.domain.enums import Category, MatchSource
from paper_trail.domain.models import Product
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import discount_line, item_line, product
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, seed_purchase, sqlite_uow_factory
from tests.support import CopyMigrated, require_id


@pytest.fixture
def engine(tmp_path: Path, copy_migrated: CopyMigrated) -> Iterator[Engine]:
    engine = create_engine(copy_migrated(tmp_path / "search.db"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def factory(engine: Engine) -> UnitOfWorkFactory:
    return sqlite_uow_factory(engine, FakeClock())


def _add(factory: UnitOfWorkFactory, name: str, **overrides: object) -> Product:
    values: dict[str, object] = {"canonical_name": name, "brand": None, "tags": ()}
    values.update(overrides)
    return in_unit(factory, lambda uow: uow.products.add(product(**values)))


def _buy(factory: UnitOfWorkFactory, *bought: Product, day: int = 1, store: str = "METRO") -> int:
    lines = [
        item_line(i, f"LINE {i}", 100, product_id=require_id(p.id), match_source=MatchSource.NEW)
        for i, p in enumerate(bought)
    ]
    stored = seed_purchase(factory, lines, store_name=store, purchased_on=date(2026, 11, day))
    return require_id(stored.id)


def test_products_rank_text_matches_first_then_category_matches_by_name(
    factory: UnitOfWorkFactory,
) -> None:
    gold = _add(factory, "Gold Kiwi", category=Category.PRODUCE)
    green = _add(factory, "Green Grapes", brand="Kiwi Farms", category=Category.PRODUCE)
    tagged = _add(factory, "Apple Juice", tags=("kiwi",), category=Category.BEVERAGES)
    box = _add(factory, "Zesty Produce Box", category=Category.OTHER)
    _add(factory, "Kiwi Sorbet")  # never bought
    _buy(factory, gold, green, tagged, box)

    kiwi = SearchPurchases(factory)("kiwi")
    produce = SearchPurchases(factory)("  PROD ")

    assert {s.product.id for s in kiwi.products} == {gold.id, green.id, tagged.id}
    assert [s.product.id for s in produce.products] == [box.id, gold.id, green.id]


def test_receipts_match_by_store_printed_text_description_or_product_name(
    factory: UnitOfWorkFactory,
) -> None:
    kiwi = _add(factory, "Gold Kiwi")
    by_product = seed_purchase(
        factory,
        [item_line(0, "1543210 GLD KW", 499, description="Fruit", product_id=require_id(kiwi.id),
                   match_source=MatchSource.NEW)],
        store_name="COSTCO WHOLESALE #1234",
        purchased_on=date(2026, 11, 5),
    )  # fmt: skip
    by_text = seed_purchase(
        factory,
        [
            item_line(0, "MILK 2L", 499, description="Milk"),
            item_line(1, "KIWI 3PK", 299, description="Kiwis"),
            discount_line(2, "KIWI SALE", -50, 1),
        ],
        store_name="METRO",
        purchased_on=date(2026, 11, 3),
    )
    by_store = seed_purchase(
        factory,
        [item_line(0, "BREAD", 299)],
        store_name="KIWI MARKET",
        purchased_on=date(2026, 10, 1),
    )
    seed_purchase(factory, [item_line(0, "BREAD", 299)], store_name="LOBLAWS")

    results = SearchPurchases(factory)("kiwi")

    assert [(m.receipt.id, m.lines) for m in results.receipts] == [
        (by_product.id, ("Fruit",)),
        (by_text.id, ("Kiwis",)),
        (by_store.id, ()),
    ]


@pytest.mark.parametrize("query", ["%", "_", "%%", "\\", '"', "NEAR(", "*"])
def test_like_wildcards_and_search_syntax_are_plain_text(
    factory: UnitOfWorkFactory, query: str
) -> None:
    seed_purchase(factory, [item_line(0, "BREAD", 299)], store_name="METRO")
    odd = seed_purchase(factory, [item_line(0, "BREAD", 299)], store_name=f"A{query}B")

    results = SearchPurchases(factory)(query)

    assert [m.receipt.id for m in results.receipts] == [odd.id]


def test_results_are_capped(factory: UnitOfWorkFactory) -> None:
    products = [_add(factory, f"Kiwi {n:02d}") for n in range(12)]
    first = date(2026, 1, 1)
    for n in range(22):
        seed_purchase(
            factory,
            [item_line(0, "BREAD", 299)],
            store_name="KIWI MARKET",
            purchased_on=first + timedelta(days=n),
        )
    _buy(factory, *products, store="LOBLAWS")

    results = SearchPurchases(factory)("kiwi")

    assert (len(results.products), len(results.receipts)) == (10, 20)


def test_limits_apply_after_filtering_so_many_matches_crowd_nothing_out(
    factory: UnitOfWorkFactory,
) -> None:
    for n in range(30):  # never bought, and better text matches than the one bought
        _add(factory, f"Kiwi {n:02d}", tags=("kiwi",))
    bought = _add(factory, "Gold Kiwi Fruit Cup")
    many = [item_line(i, "KIWI", 100) for i in range(300)]
    for day in (1, 2):
        seed_purchase(factory, many, store_name="METRO", purchased_on=date(2026, 10, day))
    newest = seed_purchase(
        factory,
        [item_line(0, "KIWI", 100, product_id=bought.id, match_source=MatchSource.NEW)],
        store_name="METRO",
        purchased_on=date(2026, 11, 1),
    )

    results = SearchPurchases(factory)("kiwi")

    assert [s.product.id for s in results.products] == [bought.id]
    assert [(m.receipt.purchased_on, len(m.lines)) for m in results.receipts] == [
        (newest.purchased_on, 1),
        (date(2026, 10, 2), 300),
        (date(2026, 10, 1), 300),
    ]


def test_a_blank_query_finds_nothing(factory: UnitOfWorkFactory) -> None:
    seed_purchase(factory, [item_line(0, "BREAD", 299)], store_name="METRO  MARKET")

    results = SearchPurchases(factory)("   ")

    assert (results.products, results.receipts) == ((), ())
