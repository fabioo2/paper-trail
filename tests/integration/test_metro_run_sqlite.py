"""The Metro receipt read by Claude, stored on a real database file (claude-extraction plan TF,
E2, E4; CE-TC-052, 053).

The graph is wired as ``bootstrap`` wires it (``tests/agent_harness.py``) over the SQLite unit
of work and LangGraph's ``CheckpointSaver`` on the same file, with the scripted reader in place
of the Anthropic adapter. Everything is read back with SQL: each item is an ITEM line at its
regular price followed by a DISCOUNT line linked to it, the department headings name the new
products' categories (``receipt_text``), and the receipt total, the sum of its lines and the sum
of the prices paid are one number.
"""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from paper_trail.adapters.sqlite.checkpoints import CheckpointSaver
from paper_trail.application.prices import BackfillPrices
from paper_trail.domain.enums import ReceiptStatus
from paper_trail.domain.money import Money
from tests.agent_harness import Harness, build_harness
from tests.extractions import METRO_KEY, METRO_TOTAL_CENTS, NOW, metro_answer
from tests.fakes.clock import FakeClock
from tests.fakes.reader import ScriptedReader
from tests.storage import sqlite_uow_factory

LINES = [
    (0, "item", 229, None),
    (1, "item", 279, None),
    (2, "item", 499, None),
    (3, "discount", -30, 2),
    (4, "item", 241, None),
    (5, "discount", -161, 4),
    (6, "item", 1399, None),
    (7, "discount", -100, 6),
    (8, "item", 599, None),
    (9, "discount", -50, 8),
    (10, "item", 1599, None),
    (11, "discount", -300, 10),
]
RECEIPT_TEXT = {"canonical_name": "receipt_text"}
NAMED_AND_SORTED = {"canonical_name": "receipt_text", "category": "receipt_text"}
ITEMS = [
    (0, "BANANAS", "produce", NAMED_AND_SORTED),
    (1, "ROMAINE HEARTS", "produce", NAMED_AND_SORTED),
    (2, "WHOLE GRAIN CRACKERS", None, RECEIPT_TEXT),
    (4, "TOMATOES ON VINE", "produce", NAMED_AND_SORTED),
    (6, "DAILY SHAMPOO", "personal_care", NAMED_AND_SORTED),
    (8, "PASTA SAUCE", None, RECEIPT_TEXT),
    (10, "TOOTHPASTE 4PK", "personal_care", NAMED_AND_SORTED),
]
PRICES = [
    (0, 229, 0, 229),
    (1, 279, 0, 279),
    (2, 499, -30, 469),
    (4, 241, -161, 80),
    (6, 1399, -100, 1299),
    (8, 599, -50, 549),
    (10, 1599, -300, 1299),
]


@pytest.fixture
def database(migrated_engine: Engine) -> Path:
    return Path(str(migrated_engine.url.database))


@pytest.fixture
def metro(migrated_engine: Engine, database: Path) -> Iterator[tuple[Harness, int]]:
    connection = sqlite3.connect(database, check_same_thread=False)
    clock = FakeClock(NOW)
    harness = build_harness(
        ScriptedReader(metro_answer()),
        clock=clock,
        uow_factory=sqlite_uow_factory(migrated_engine, clock),
        checkpointer=CheckpointSaver(connection),
    )
    graph = harness.process_image("metro").graph
    assert graph is not None
    assert graph.status is ReceiptStatus.COMPLETED
    yield harness, graph.receipt_id
    connection.close()


def _rows(database: Path, sql: str, *params: object) -> list[tuple[Any, ...]]:
    with sqlite3.connect(database) as connection:
        return connection.execute(sql, params).fetchall()


def test_ce_tc_052_the_metro_receipt_is_stored_item_by_item(
    metro: tuple[Harness, int], database: Path
) -> None:
    _, receipt_id = metro

    receipts = _rows(database, "SELECT store_key, total_cents FROM receipts")
    lines = _rows(
        database,
        "SELECT position, kind, line_total_cents, applies_to_position FROM line_items"
        " WHERE receipt_id = ? ORDER BY position",
        receipt_id,
    )
    items = _rows(
        database,
        "SELECT l.position, l.raw_text, p.category, p.field_sources FROM line_items l"
        " JOIN products p ON p.id = l.product_id WHERE l.kind = 'item' ORDER BY l.position",
    )
    prices = _rows(
        database,
        "SELECT l.position, o.line_total_cents, o.discount_cents, o.paid_cents"
        " FROM price_observations o JOIN line_items l ON l.id = o.line_item_id"
        " ORDER BY l.position",
    )

    assert receipts == [(METRO_KEY, METRO_TOTAL_CENTS)]
    assert lines == LINES
    assert [(p, raw, cat, json.loads(src)) for p, raw, cat, src in items] == ITEMS
    assert prices == PRICES
    assert _rows(database, "SELECT COUNT(*) FROM products") == [(7,)]
    assert _rows(database, "SELECT COUNT(*) FROM line_items WHERE product_id IS NULL") == [(5,)]


def test_ce_tc_052_total_lines_and_prices_paid_are_one_number(
    metro: tuple[Harness, int], database: Path
) -> None:
    _, receipt_id = metro

    (views,) = _rows(
        database,
        "SELECT r.total_cents,"
        " (SELECT SUM(line_total_cents) FROM line_items WHERE receipt_id = r.id),"
        " (SELECT SUM(paid_cents) FROM price_observations WHERE receipt_id = r.id)"
        " FROM receipts r WHERE r.id = ?",
        receipt_id,
    )

    assert views == (METRO_TOTAL_CENTS,) * 3


def test_ce_tc_053_a_metro_saving_shows_like_a_costco_discount(
    metro: tuple[Harness, int], database: Path
) -> None:
    """The crackers' history reads regular 4.99, discount -0.30, paid 4.69; Plan A's ``prices
    backfill`` finds every link already made and changes no row."""
    harness, receipt_id = metro
    crackers = harness.line(receipt_id, 2).product_id
    assert crackers is not None
    tables = ("line_items", "price_observations", "receipts")
    before = {table: _rows(database, f"SELECT * FROM {table} ORDER BY id") for table in tables}  # noqa: S608 - fixed names

    (seen,) = harness.price_history(crackers)
    BackfillPrices(harness.uow_factory)(apply=True)

    assert (seen.line_total, seen.discount, seen.paid) == (
        Money(499, "CAD"),
        Money(-30, "CAD"),
        Money(469, "CAD"),
    )
    after = {table: _rows(database, f"SELECT * FROM {table} ORDER BY id") for table in tables}  # noqa: S608 - fixed names
    assert after == before
