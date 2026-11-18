"""Revision 0010, line units and failure reasons: ``line_items.unit`` and
``receipts.failure_reason`` (hardening H3-01, H3-02).

A real SQLite file in ``tmp_path``, seeded at 0009 with raw SQL: a FAILED receipt with an ITEM
line and its linked DISCOUNT line.

- Upgrade: every row as it was, the new columns NULL; the line items' indexes, foreign keys and
  FTS triggers are still there (the full-text index follows a new line); each column refuses a
  value outside its list.
- Downgrade: the 0009 schema text and rows again; upgrading again works.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, text

from paper_trail.adapters.sqlite.engine import create_engine
from paper_trail.adapters.sqlite.migrations import alembic_config, current_revision

NOW = "2026-11-18T12:00:00.000000+00:00"
INSERT_LINE = (
    "INSERT INTO line_items (receipt_id, position, kind, raw_text, description, quantity,"
    " unit_price_cents, line_total_cents, applies_to_position) VALUES (1, :position, :kind,"
    " :raw_text, :raw_text, '0.452', NULL, :total, :applies_to)"
)


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "paper_trail.db"


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    engine = create_engine(database)
    yield engine
    engine.dispose()


def _schema(database: Path) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(
            "SELECT type, name, sql FROM sqlite_master"
            " WHERE tbl_name IN ('line_items', 'receipts') ORDER BY name"
        ).fetchall()


def _rows(engine: Engine, table: str) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings()  # noqa: S608
        return [dict(row) for row in rows]


def _line(engine: Engine, position: int, raw_text: str, **values: object) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(INSERT_LINE),
            {"position": position, "kind": "item", "raw_text": raw_text, "total": 497}
            | {"applies_to": None}
            | values,
        )


def _seed_0009(database: Path, engine: Engine) -> list[tuple[str, str, str]]:
    command.upgrade(alembic_config(database), "0009")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO receipts (id, status, stage, store_name, store_key, currency,"
                " created_at, updated_at) VALUES (1, 'failed', 'failed', 'METRO', 'metro', 'CAD',"
                " :now, :now)"
            ),
            {"now": NOW},
        )
    _line(engine, 0, "BANANAS")
    _line(engine, 1, "BANANAS", kind="discount", total=-100, applies_to=0)
    return _schema(database)


def test_h3_01_upgrade_keeps_every_row_with_null_units_and_reasons(
    database: Path, engine: Engine
) -> None:
    schema_0009 = _seed_0009(database, engine)
    receipts_before, lines_before = _rows(engine, "receipts"), _rows(engine, "line_items")

    command.upgrade(alembic_config(database), "0010")

    assert current_revision(engine) == "0010"
    assert _rows(engine, "receipts") == [row | {"failure_reason": None} for row in receipts_before]
    assert _rows(engine, "line_items") == [row | {"unit": None} for row in lines_before]
    kept = [(kind, name) for kind, name, _ in schema_0009]
    assert [(kind, name) for kind, name, _ in _schema(database)] == kept
    _line(engine, 2, "PEACHES")
    with engine.begin() as connection:
        connection.execute(text("UPDATE line_items SET unit = 'kg' WHERE id = 3"))
        found = connection.execute(
            text("SELECT rowid FROM line_items_fts WHERE line_items_fts MATCH 'peaches'")
        ).scalars()
        assert list(found) == [3]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE line_items SET unit = 'g' WHERE id = 1",
        "UPDATE receipts SET failure_reason = 'crashed' WHERE id = 1",
    ],
    ids=["unit", "failure-reason"],
)
def test_h3_01_the_new_columns_refuse_other_values(
    database: Path, engine: Engine, statement: str
) -> None:
    _seed_0009(database, engine)
    command.upgrade(alembic_config(database), "0010")

    with pytest.raises(Exception, match="constraint failed"), engine.begin() as connection:
        connection.execute(text(statement))


def test_h3_02_downgrade_gives_back_0009_and_upgrades_again(database: Path, engine: Engine) -> None:
    schema_0009 = _seed_0009(database, engine)
    receipts_before, lines_before = _rows(engine, "receipts"), _rows(engine, "line_items")
    config = alembic_config(database)
    command.upgrade(config, "0010")

    command.downgrade(config, "0009")

    assert current_revision(engine) == "0009"
    assert _schema(database) == schema_0009
    assert (_rows(engine, "receipts"), _rows(engine, "line_items")) == (
        receipts_before,
        lines_before,
    )
    command.upgrade(config, "0010")
    assert current_revision(engine) == "0010"
