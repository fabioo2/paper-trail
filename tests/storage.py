"""Helpers shared by the Stage 1 storage suites (contract, integration, e2e)."""

import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Engine

from paper_trail.application.products import price_observation as observed
from paper_trail.domain.enums import LineKind, ProcessingStage, ReceiptStatus
from paper_trail.domain.models import LineItem, Receipt
from paper_trail.domain.rules.normalization import normalize_store_name
from paper_trail.ports.clock import Clock
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.factories import (
    CREATED_AT,
    PURCHASED_ON,
    SIMPLE_SUBTOTAL,
    SIMPLE_TAX,
    SIMPLE_TOTAL,
    STORE_NAME,
    cad,
    price_observation,
    product,
    receipt,
    simple_lines,
)
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


def seed_pre_feature(  # noqa: PLR0913 - a test builder: keyword-only overrides with defaults
    factory: UnitOfWorkFactory,
    lines: Sequence[LineItem] | None = None,
    *,
    totals: tuple[int, int, int] = (SIMPLE_SUBTOTAL, SIMPLE_TAX, SIMPLE_TOTAL),
    created_at: datetime = CREATED_AT,
    purchased_on: date | None = PURCHASED_ON,
    status: ReceiptStatus = ReceiptStatus.COMPLETED,
    stage: ProcessingStage = ProcessingStage.DONE,
    unmatched: frozenset[int] = frozenset(),
) -> Receipt:
    """A receipt stored as before discounts were linked, the way migration 0004 leaves existing
    data: ``lines`` (SIMPLE by default) with no link; every ITEM line not in ``unmatched`` has
    its own product and, when the receipt is dated, an observation at the regular price with
    discount 0."""
    subtotal, tax, total = totals

    def store(uow: UnitOfWork) -> Receipt:
        items = tuple(
            replace(
                line,
                product_id=require_id(uow.products.add(product(canonical_name=line.raw_text)).id),
            )
            if line.kind is LineKind.ITEM and line.position not in unmatched
            else line
            for line in (simple_lines() if lines is None else lines)
        )
        stored = uow.receipts.add(
            receipt(
                status=status,
                stage=stage,
                purchased_on=purchased_on,
                subtotal=cad(subtotal),
                tax=cad(tax),
                total=cad(total),
                items=items,
                created_at=created_at,
                updated_at=created_at,
            )
        )
        if purchased_on is not None:
            uow.prices.add_many(
                [
                    price_observation(
                        product_id=require_id(item.product_id),
                        receipt_id=require_id(stored.id),
                        line_item_id=require_id(item.id),
                        observed_on=purchased_on,
                        quantity=item.quantity,
                        unit_price=item.unit_price or item.line_total.divided_by(item.quantity),
                        line_total=item.line_total,
                    )
                    for item in stored.items
                    if item.product_id is not None
                ]
            )
        return stored

    return in_unit(factory, store)


def seed_purchase(
    factory: UnitOfWorkFactory,
    lines: Sequence[LineItem],
    *,
    store_name: str = STORE_NAME,
    purchased_on: date | None = PURCHASED_ON,
    created_at: datetime = CREATED_AT,
) -> Receipt:
    """A COMPLETED receipt at ``store_name`` with ``lines`` stored as given (link a line with
    ``product_id=`` and ``match_source=``), and, when dated, an observation per linked ITEM line
    at its regular price with the discount of the DISCOUNT line that applies to it."""

    def store(uow: UnitOfWork) -> Receipt:
        stored = uow.receipts.add(
            receipt(
                store_name=store_name,
                store_key=normalize_store_name(store_name),
                purchased_on=purchased_on,
                items=tuple(lines),
                created_at=created_at,
                updated_at=created_at,
            )
        )
        if purchased_on is not None:
            uow.prices.add_many(
                [
                    observed(stored, item, purchased_on)
                    for item in stored.items
                    if item.kind is LineKind.ITEM and item.product_id is not None
                ]
            )
        return stored

    return in_unit(factory, store)
