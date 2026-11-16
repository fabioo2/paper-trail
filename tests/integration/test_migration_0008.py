"""Revision 0008, product lookups (claude-extraction plan V5, V10; CE-TC-202..205).

A real SQLite file in ``tmp_path``, seeded at 0007 with raw SQL: products stamped ``off_llm``,
``off``, ``person``, ``legacy_model`` and ``receipt_text`` in various fields.

- Upgrade: ``product_lookups`` exists; every ``off_llm`` stamp is ``llm``, every other stamp and
  every value as it was; a former ``off_llm`` product is no longer confirmed.
- ``product_lookups`` takes a hit with a payload and a miss without one, and refuses the rest.
- Downgrade: ``llm`` is ``off_llm`` again and ``upc`` is ``off``; the table is gone; up, down,
  up gives the same rows.
"""

import json
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
from paper_trail.domain.models import is_confirmed
from tests.fakes.clock import FakeClock
from tests.storage import sqlite_uow_factory

NOW = "2026-11-15T12:00:00.000000+00:00"
SEEDED = {
    1: ("Eggs", {"canonical_name": "off_llm", "brand": "off_llm", "size": "off"}),
    2: ("Milk", {"canonical_name": "off", "category": "legacy_model"}),
    3: ("Bread", {"canonical_name": "person", "brand": "off_llm", "tags": "receipt_text"}),
    4: ("Kiwi", {"canonical_name": "receipt_text"}),
    5: ("Nothing stamped", {}),
}


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return tmp_path / "paper_trail.db"


@pytest.fixture
def engine(database: Path) -> Iterator[Engine]:
    engine = create_engine(database)
    yield engine
    engine.dispose()


def _seed_0007(database: Path, engine: Engine) -> None:
    command.upgrade(alembic_config(database), "0007")
    with engine.begin() as connection:
        for product_id, (name, sources) in SEEDED.items():
            connection.execute(
                text(
                    "INSERT INTO products (id, canonical_name, brand, created_at, field_sources)"
                    " VALUES (:id, :name, 'Acme', :now, :sources)"
                ),
                {"id": product_id, "name": name, "now": NOW, "sources": json.dumps(sources)},
            )


def _products(engine: Engine) -> dict[int, tuple[str, str | None, dict[str, str]]]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, canonical_name, brand, field_sources FROM products ORDER BY id")
        ).all()
    return {row[0]: (row[1], row[2], json.loads(row[3])) for row in rows}


def _tables(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def _renamed(renames: dict[str, str]) -> dict[int, tuple[str, str | None, dict[str, str]]]:
    return {
        product_id: (
            name,
            "Acme",
            {key: renames.get(value, value) for key, value in sources.items()},
        )
        for product_id, (name, sources) in SEEDED.items()
    }


def test_upgrade_folds_off_llm_into_llm_and_adds_the_lookup_table(
    database: Path, engine: Engine
) -> None:
    _seed_0007(database, engine)

    command.upgrade(alembic_config(database), "head")

    assert current_revision(engine) == "0008"
    assert {"product_lookups", "lookup_requests"} <= _tables(database)
    assert _products(engine) == _renamed({"off_llm": "llm"})
    with sqlite_uow_factory(engine, FakeClock())(read_only=True) as uow:
        confirmed = {
            product_id: is_confirmed(stored)
            for product_id in SEEDED
            if (stored := uow.products.get(product_id)) is not None
        }
    assert confirmed == {1: False, 2: True, 3: True, 4: False, 5: False}


def test_downgrade_gives_the_old_code_sources_it_knows(database: Path, engine: Engine) -> None:
    _seed_0007(database, engine)
    config = alembic_config(database)
    command.upgrade(config, "head")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE products SET field_sources = :sources WHERE id = 4"),
            {"sources": json.dumps({"canonical_name": "upc", "size": "llm"})},
        )
        connection.execute(
            text(
                "INSERT INTO product_lookups (code, source, state, payload, fetched_at)"
                " VALUES ('4006381333931', 'off', 'not_found', NULL, :now)"
            ),
            {"now": NOW},
        )

    command.downgrade(config, "0007")

    expected = _renamed({})
    expected[4] = ("Kiwi", "Acme", {"canonical_name": "off", "size": "off_llm"})
    assert current_revision(engine) == "0007"
    assert {"product_lookups", "lookup_requests"}.isdisjoint(_tables(database))
    assert _products(engine) == expected

    command.upgrade(config, "head")
    command.downgrade(config, "0007")

    assert _products(engine) == expected


ACCEPTED = [
    ("4006381333931", "off", "hit", '{"name": "Eggs"}'),
    ("4006381333931", "upcitemdb", "not_found", None),
]
REFUSED: list[tuple[Any, ...]] = [
    ("", "off", "not_found", None),
    ("40063813339X1", "off", "not_found", None),
    ("4006381333931", "costco", "not_found", None),
    ("4006381333931", "off", "error", None),
    ("4006381333931", "off", "hit", None),
    ("4006381333931", "off", "not_found", '{"name": "Eggs"}'),
    ("4006381333931", "off", "hit", '{"name": "a second row"}'),
]


def test_product_lookups_constraints(database: Path, engine: Engine) -> None:
    command.upgrade(alembic_config(database), "head")
    insert = text(
        "INSERT INTO product_lookups (code, source, state, payload, fetched_at)"
        " VALUES (:code, :source, :state, :payload, :now)"
    )
    with engine.begin() as connection:
        for code, source, state, payload in ACCEPTED:
            connection.execute(
                insert,
                {"code": code, "source": source, "state": state, "payload": payload, "now": NOW},
            )
    for code, source, state, payload in REFUSED:
        with pytest.raises(Exception, match="constraint failed"), engine.begin() as connection:
            connection.execute(
                insert,
                {"code": code, "source": source, "state": state, "payload": payload, "now": NOW},
            )
