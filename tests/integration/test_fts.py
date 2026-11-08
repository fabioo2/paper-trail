"""FTS5 specifics of the text search adapter (S1-AC-16, S1-AC-13).

The query semantics shared with the fake are in ``tests/contract/test_text_search_index.py``.
Here: the AFTER INSERT/UPDATE/DELETE triggers keep the external-content FTS tables in step with
rows changed by any SQL (not only through the repositories), and scores are negated bm25.
"""

from decimal import Decimal

import pytest
from sqlalchemy import Engine, text

from paper_trail.domain.models import Receipt
from paper_trail.ports.search import HitKind, TextHit
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import line_item, product, receipt
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, sqlite_uow_factory
from tests.support import require_id


@pytest.fixture
def uow_factory(migrated_engine: Engine) -> UnitOfWorkFactory:
    return sqlite_uow_factory(migrated_engine, FakeClock())


@pytest.fixture
def stored(uow_factory: UnitOfWorkFactory) -> Receipt:
    items = (
        line_item(position=0, raw_text="KS ORG EGGS 24CT",
                  description="Kirkland Organic Eggs 24 ct"),
        line_item(position=1, raw_text="BNLS CHKN THGH", description="Boneless chicken thighs",
                  quantity=Decimal("1.2")),
    )  # fmt: skip
    return in_unit(uow_factory, lambda uow: uow.receipts.add(receipt(items=items)))


def _search(uow_factory: UnitOfWorkFactory, query: str) -> tuple[TextHit, ...]:
    with uow_factory() as uow:
        return uow.text_search.search(query, limit=20)


def _execute(engine: Engine, sql: str, **params: object) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def test_s1_ac_16_scores_are_negated_bm25(uow_factory: UnitOfWorkFactory, stored: Receipt) -> None:
    """PLAN 3.4: bm25() is negative (lower is better); the port's score is its negation."""
    in_unit(uow_factory, lambda uow: uow.products.add(product()))

    hits = _search(uow_factory, "eggs")

    assert len(hits) == 2
    assert all(hit.score > 0 for hit in hits)


def test_s1_ac_16_triggers_follow_sql_updates_and_deletes(
    uow_factory: UnitOfWorkFactory, migrated_engine: Engine, stored: Receipt
) -> None:
    """S1-AC-16: the triggers keep line_items_fts and products_fts in step with their tables."""
    eggs = require_id(stored.items[0].id)
    saved_product = require_id(in_unit(uow_factory, lambda uow: uow.products.add(product())).id)

    _execute(
        migrated_engine,
        "UPDATE line_items SET description = 'Free range duck eggs', raw_text = 'DUCK EGGS'"
        " WHERE id = :id",
        id=eggs,
    )
    _execute(
        migrated_engine,
        "UPDATE products SET canonical_name = 'Duck eggs', brand = NULL, tags = '[]'"
        " WHERE id = :id",
        id=saved_product,
    )

    assert _search(uow_factory, "organic") == ()
    assert _search(uow_factory, "kirkland") == ()
    assert {(hit.kind, hit.id) for hit in _search(uow_factory, "duck")} == {
        (HitKind.LINE_ITEM, eggs),
        (HitKind.PRODUCT, saved_product),
    }

    _execute(migrated_engine, "DELETE FROM line_items WHERE id = :id", id=eggs)
    _execute(migrated_engine, "DELETE FROM products WHERE id = :id", id=saved_product)

    assert _search(uow_factory, "duck") == ()


def test_s1_ac_16_triggers_index_rows_inserted_by_sql(
    uow_factory: UnitOfWorkFactory, migrated_engine: Engine, stored: Receipt
) -> None:
    """S1-AC-16: an INSERT that bypasses the repositories is searchable at once."""
    _execute(
        migrated_engine,
        "INSERT INTO line_items (receipt_id, position, kind, raw_text, description, quantity,"
        " line_total_cents) VALUES (:receipt_id, 9, 'item', 'OAT MILK 1.75L', 'Oat milk', '1',"
        " 449)",
        receipt_id=stored.id,
    )

    assert [hit.kind for hit in _search(uow_factory, "oat milk")] == [HitKind.LINE_ITEM]


def test_s1_ac_13_deleting_a_receipt_removes_its_items_from_the_index(
    uow_factory: UnitOfWorkFactory, migrated_engine: Engine, stored: Receipt
) -> None:
    """S1-AC-16 with S1-AC-13: the cascade delete fires the FTS delete trigger."""
    _execute(migrated_engine, "DELETE FROM receipts WHERE id = :id", id=stored.id)

    assert _search(uow_factory, "eggs") == ()
    assert _search(uow_factory, "chicken") == ()
