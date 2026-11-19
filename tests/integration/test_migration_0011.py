"""Revision 0011, product ids never reused: ``products.id`` becomes AUTOINCREMENT (cleanup D).

A real SQLite file in ``tmp_path``, seeded at 0010 with raw SQL: three products, a receipt
line, a price, a code and an alias naming them.

- Upgrade: the database passes ``integrity_check``, ``foreign_key_check`` and the products FTS
  ``integrity-check``; every row and id is as it was, and every other schema object (indexes,
  foreign keys, FTS triggers) is unchanged; a deleted product's id, even the highest, is never
  given to the next product, and the full-text index follows the new one.
- Downgrade: the 0010 schema text again; upgrading again works.
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

NOW = "2026-11-19T12:00:00.000000+00:00"
TABLES = ("products", "line_items", "price_observations", "product_codes", "product_aliases")


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
            "SELECT type, name, sql FROM sqlite_master WHERE name <> 'sqlite_sequence'"
            " ORDER BY name"
        ).fetchall()


def _rows(engine: Engine) -> dict[str, list[dict[str, Any]]]:
    with engine.connect() as connection:
        return {
            table: [
                dict(row)
                for row in connection.execute(
                    text(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608 - fixed names
                ).mappings()
            ]
            for table in TABLES
        }


def _add_product(engine: Engine, name: str) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "INSERT INTO products (canonical_name, created_at) VALUES (:name, :now)"
                    " RETURNING id"
                ),
                {"name": name, "now": NOW},
            ).scalar_one()
        )


def _delete_product(engine: Engine, product_id: int) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM products WHERE id = :id"), {"id": product_id})


def _seed_0010(database: Path, engine: Engine) -> list[tuple[str, str, str]]:
    command.upgrade(alembic_config(database), "0010")
    for name in ("Red peppers", "Bell peppers", "Butter"):
        _add_product(engine, name)
    with engine.begin() as connection:
        for statement in [
            "INSERT INTO receipts (id, status, stage, store_name, store_key, purchased_on,"
            " currency, created_at, updated_at) VALUES (1, 'completed', 'done', 'COSTCO',"
            " 'costco', '2026-11-01', 'CAD', :now, :now)",
            "INSERT INTO line_items (receipt_id, position, kind, raw_text, description,"
            " quantity, line_total_cents, product_id) VALUES (1, 0, 'item', 'RED PEPPERS',"
            " 'Red Peppers', '1', 599, 1)",
            "INSERT INTO price_observations (product_id, receipt_id, line_item_id, store_key,"
            " observed_on, quantity, unit_price_cents, line_total_cents, currency, paid_cents)"
            " VALUES (1, 1, 1, 'costco', '2026-11-01', '1', 599, 599, 'CAD', 599)",
            "INSERT INTO product_codes (product_id, scheme, store_key, code, source, created_at,"
            " updated_at) VALUES (2, 'store', 'costco', '1362394', 'manual', :now, :now)",
            "INSERT INTO product_aliases (product_id, alias, store_key, source, created_at)"
            " VALUES (3, 'butter', 'costco', 'matched', :now)",
        ]:
            connection.execute(text(statement), {"now": NOW})
    return _schema(database)


def _checks(engine: Engine) -> tuple[object, ...]:
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO products_fts(products_fts) VALUES ('integrity-check')")
        )
        return (
            connection.execute(text("PRAGMA integrity_check")).scalars().all(),
            connection.execute(text("PRAGMA foreign_key_check")).all(),
        )


def test_upgrade_keeps_every_row_and_object_and_never_reuses_an_id(
    database: Path, engine: Engine
) -> None:
    schema_0010 = _seed_0010(database, engine)
    rows = _rows(engine)

    command.upgrade(alembic_config(database), "0011")

    assert current_revision(engine) == "0011"
    assert _checks(engine) == (["ok"], [])
    assert _rows(engine) == rows
    assert [entry for entry in _schema(database) if entry[1] != "products"] == [
        entry for entry in schema_0010 if entry[1] != "products"
    ]
    _delete_product(engine, 3)
    assert _add_product(engine, "Paper towels") == 4
    assert _add_product(engine, "Napkins") == 5
    with engine.connect() as connection:
        found = connection.execute(
            text("SELECT rowid FROM products_fts WHERE products_fts MATCH 'towels'")
        ).scalars()
        assert list(found) == [4]
    assert _checks(engine) == (["ok"], [])


def test_downgrade_gives_back_0010_and_upgrades_again(database: Path, engine: Engine) -> None:
    schema_0010 = _seed_0010(database, engine)
    config = alembic_config(database)
    command.upgrade(config, "0011")

    command.downgrade(config, "0010")

    assert current_revision(engine) == "0010"
    assert _schema(database) == schema_0010
    command.upgrade(config, "0011")
    assert current_revision(engine) == "0011"
    assert _checks(engine) == (["ok"], [])
