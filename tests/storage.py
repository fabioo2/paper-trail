"""Helpers shared by the Stage 1 storage suites (contract, integration, e2e)."""

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

from sqlalchemy import Engine

from paper_trail.ports.clock import Clock
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.factories import product, receipt
from tests.support import require_id


def in_unit[T](factory: UnitOfWorkFactory, action: Callable[[UnitOfWork], T]) -> T:
    """Run ``action`` in a fresh unit of work and commit it."""
    with factory() as uow:
        result = action(uow)
        uow.commit()
    return result


def sqlite_uow_factory(engine: Engine, clock: Clock) -> UnitOfWorkFactory:
    # Imported here so that unit tests can use the other helpers without the SQLite adapters.
    from paper_trail.adapters.sqlite.unit_of_work import SqliteUnitOfWork  # noqa: PLC0415

    def factory(*, read_only: bool = False) -> SqliteUnitOfWork:
        return SqliteUnitOfWork(engine, clock, read_only=read_only)

    return factory


def table_values(database: Path) -> Iterator[tuple[str, str, str]]:
    """(table, column, value) for every non-NULL cell of every table in the database file.

    BLOBs (FTS5 index pages, vectors) are decoded as Latin-1 so that digits stored in them are
    still visible to a text scan.
    """
    with closing(sqlite3.connect(database)) as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        for table in tables:
            try:
                cursor = connection.execute(f'SELECT * FROM "{table}"')  # noqa: S608 - names from sqlite_master
            except sqlite3.OperationalError:
                continue  # virtual tables whose module (vec0) is not loaded in this connection
            columns = [description[0] for description in cursor.description]
            for row in cursor:
                for column, value in zip(columns, row, strict=True):
                    if isinstance(value, bytes):
                        yield table, column, value.decode("latin-1")
                    elif value is not None:
                        yield table, column, str(value)


def seed_product_and_receipt(factory: UnitOfWorkFactory) -> tuple[int, int, list[int]]:
    """Store the default product and receipt; return (product_id, receipt_id, line_item_ids)."""

    def seed(uow: UnitOfWork) -> tuple[int, int, list[int]]:
        saved_product = uow.products.add(product())
        saved_receipt = uow.receipts.add(receipt())
        return (
            require_id(saved_product.id),
            require_id(saved_receipt.id),
            [require_id(item.id) for item in saved_receipt.items],
        )

    return in_unit(factory, seed)
