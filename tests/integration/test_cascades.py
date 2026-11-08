"""Foreign-key actions of the core schema (S1-AC-13)."""

from dataclasses import dataclass, replace

import pytest
from sqlalchemy import Engine, text

from paper_trail.domain.enums import CorrectionKind, JobKind, ReviewReason
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.factories import correction, price_observation, product, product_alias, receipt
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, sqlite_uow_factory
from tests.support import require_id

VECTOR = [1.0] + [0.0] * 767


@dataclass(frozen=True)
class Graph:
    receipt_id: int
    other_receipt_id: int
    product_id: int
    line_ids: list[int]


@pytest.fixture
def uow_factory(migrated_engine: Engine) -> UnitOfWorkFactory:
    return sqlite_uow_factory(migrated_engine, FakeClock())


@pytest.fixture
def graph(uow_factory: UnitOfWorkFactory) -> Graph:
    """A receipt whose lines link to a product, with every kind of dependent row."""

    def build(uow: UnitOfWork) -> Graph:
        product_id = require_id(uow.products.add(product()).id)
        uow.products.add_alias(product_alias(product_id))
        uow.vectors.upsert(product_id, VECTOR)
        linked = tuple(replace(item, product_id=product_id) for item in receipt().items)
        saved = uow.receipts.add(receipt(items=linked))
        other = uow.receipts.add(receipt())
        receipt_id = require_id(saved.id)
        line_ids = [require_id(item.id) for item in saved.items]
        uow.prices.add_many(
            [
                price_observation(
                    product_id=product_id, receipt_id=receipt_id, line_item_id=line_ids[0]
                )
            ]
        )
        uow.jobs.enqueue(JobKind.PROCESS_RECEIPT, receipt_id, max_attempts=3)
        uow.reviews.open(receipt_id, ReviewReason.LOW_CONFIDENCE, {"confidence": 0.3}, None)
        uow.corrections.add(
            correction(kind=CorrectionKind.FIELD, receipt_id=receipt_id, line_item_id=line_ids[0])
        )
        return Graph(receipt_id, require_id(other.id), product_id, line_ids)

    return in_unit(uow_factory, build)


def _count(engine: Engine, sql: str, **params: object) -> int:
    with engine.connect() as connection:
        value: int = connection.execute(text(sql), params).scalar_one()
    return value


def _delete(engine: Engine, table: str, row_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(text(f"DELETE FROM {table} WHERE id = :id"), {"id": row_id})  # noqa: S608


def test_s1_ac_13_deleting_a_receipt_cascades(migrated_engine: Engine, graph: Graph) -> None:
    """S1-AC-13: line items, price observations, jobs and review items go with the receipt."""
    _delete(migrated_engine, "receipts", graph.receipt_id)

    for table in ("line_items", "price_observations", "jobs", "review_items"):
        query = f"SELECT count(*) FROM {table} WHERE receipt_id = :id"  # noqa: S608
        assert _count(migrated_engine, query, id=graph.receipt_id) == 0, table
    assert _count(migrated_engine, "SELECT count(*) FROM receipts") == 1
    assert _count(migrated_engine, "SELECT count(*) FROM line_items") == 2, "other receipt kept"
    assert _count(migrated_engine, "SELECT count(*) FROM products") == 1


def test_s1_ac_13_deleting_a_receipt_keeps_corrections_without_their_links(
    migrated_engine: Engine, graph: Graph
) -> None:
    """PLAN 3.5: corrections outlive receipts; receipt_id and line_item_id become NULL."""
    _delete(migrated_engine, "receipts", graph.receipt_id)

    with migrated_engine.connect() as connection:
        rows = connection.execute(text("SELECT receipt_id, line_item_id FROM corrections")).all()
    assert [tuple(row) for row in rows] == [(None, None)]


def test_s1_ac_13_deleting_a_product_unlinks_and_cascades(
    migrated_engine: Engine, uow_factory: UnitOfWorkFactory, graph: Graph
) -> None:
    """S1-AC-13: line_items.product_id -> NULL; aliases, observations and the vector go."""
    _delete(migrated_engine, "products", graph.product_id)

    assert (
        _count(migrated_engine, "SELECT count(*) FROM line_items WHERE product_id IS NOT NULL") == 0
    )
    assert _count(migrated_engine, "SELECT count(*) FROM line_items") == 4, "lines are kept"
    assert _count(migrated_engine, "SELECT count(*) FROM product_aliases") == 0
    assert _count(migrated_engine, "SELECT count(*) FROM price_observations") == 0
    assert _count(migrated_engine, "SELECT count(*) FROM product_vectors") == 0
    with uow_factory() as uow:
        assert uow.vectors.nearest(VECTOR, k=5) == ()
        assert uow.products.find_by_alias("ks org eggs 24ct", "costco") is None


def test_s1_ac_13_deleting_a_line_item_cascades_to_its_observation(
    migrated_engine: Engine, graph: Graph
) -> None:
    """PLAN 3.5: price_observations.line_item_id is ON DELETE CASCADE."""
    _delete(migrated_engine, "line_items", graph.line_ids[0])

    assert _count(migrated_engine, "SELECT count(*) FROM price_observations") == 0
