"""``BackfillPrices`` on real SQLite: one unit of work per receipt (discounts plan R9, A6).

Each receipt is re-read, attributed and written inside one ``BEGIN IMMEDIATE`` unit, so a
failure rolls that receipt back whole, a rerun completes the rest (idempotent), and no other
writer can interleave between the read and the writes.
"""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from itertools import count
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from paper_trail.adapters.sqlite.receipt_repository import SqliteReceiptRepository
from paper_trail.adapters.sqlite.unit_of_work import SqliteUnitOfWork
from paper_trail.application.prices import BackfillPrices, PriceBackfill
from paper_trail.domain.enums import LineKind
from paper_trail.domain.models import Receipt
from paper_trail.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from tests.factories import (
    SIMPLE_SUBTOTAL,
    SIMPLE_TAX,
    SIMPLE_TOTAL,
    at,
    cad,
    price_observation,
    product,
    receipt,
    simple_lines,
)
from tests.fakes.clock import FakeClock
from tests.storage import in_unit, sqlite_uow_factory
from tests.support import require_id

# (receipt id, discount position, item position, new link, price updated) per discount
type Outcome = list[tuple[int, int, int | None, bool, bool]]
# per receipt: (discount line's applies_to_position, eggs observation's discount and paid)
type State = dict[int, tuple[int | None, int, int]]


class _Killed(BaseException):
    """Stands for the process being killed: not an Exception, so nothing catches it."""


def _store_pre_feature(uow_factory: UnitOfWorkFactory, created_at: datetime) -> int:
    """SIMPLE stored as before the feature: no link, every item matched and observed at
    discount 0."""

    def seed(uow: UnitOfWork) -> int:
        product_id = require_id(uow.products.add(product()).id)
        stored = uow.receipts.add(
            receipt(
                items=tuple(
                    replace(line, product_id=product_id) if line.kind is LineKind.ITEM else line
                    for line in simple_lines()
                ),
                subtotal=cad(SIMPLE_SUBTOTAL),
                tax=cad(SIMPLE_TAX),
                total=cad(SIMPLE_TOTAL),
                created_at=created_at,
                updated_at=created_at,
            )
        )
        receipt_id = require_id(stored.id)
        uow.prices.add_many(
            [
                price_observation(
                    product_id=product_id,
                    receipt_id=receipt_id,
                    line_item_id=require_id(line.id),
                    quantity=line.quantity,
                    unit_price=line.unit_price,
                    line_total=line.line_total,
                )
                for line in stored.items
                if line.kind is LineKind.ITEM
            ]
        )
        return receipt_id

    return in_unit(uow_factory, seed)


def _state(engine: Engine) -> State:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT d.receipt_id, d.applies_to_position, o.discount_cents, o.paid_cents"
                " FROM line_items d"
                " JOIN line_items e ON e.receipt_id = d.receipt_id AND e.position = 0"
                " JOIN price_observations o ON o.line_item_id = e.id"
                " WHERE d.kind = 'discount'"
            )
        ).all()
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


def _outcome(backfill: PriceBackfill) -> Outcome:
    return [
        (
            entry.receipt_id,
            entry.discount.position,
            None if entry.item is None else entry.item.position,
            entry.new_link,
            entry.price_updated,
        )
        for entry in backfill.discounts
    ]


def _observation_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.execute(text("SELECT count(*) FROM price_observations")).scalar_one())


def _killing_factory(engine: Engine, clock: FakeClock, kill_at: int) -> UnitOfWorkFactory:
    """Units of work whose ``kill_at``-th commit (a write unit's) is killed before it lands."""
    commits = count(1)

    class KilledOnCommit(SqliteUnitOfWork):
        def commit(self) -> None:
            if next(commits) == kill_at:
                raise _Killed
            super().commit()

    def factory(*, read_only: bool = False) -> SqliteUnitOfWork:
        return KilledOnCommit(engine, clock, read_only=read_only)

    return factory


LINKED: tuple[int | None, int, int] = (0, -500, 399)
UNLINKED: tuple[int | None, int, int] = (None, 0, 899)


def test_tc_84_a_backfill_killed_mid_run_leaves_whole_receipts_and_reruns_cleanly(
    migrated_engine: Engine,
) -> None:
    clock = FakeClock()
    uow_factory = sqlite_uow_factory(migrated_engine, clock)
    oldest = _store_pre_feature(uow_factory, at(0))
    middle = _store_pre_feature(uow_factory, at(10))
    newest = _store_pre_feature(uow_factory, at(20))

    with pytest.raises(_Killed):
        BackfillPrices(_killing_factory(migrated_engine, clock, kill_at=2))(apply=True)
    after_kill = _state(migrated_engine)
    rerun = BackfillPrices(uow_factory)(apply=True)
    after_rerun = _state(migrated_engine)
    third = BackfillPrices(uow_factory)(apply=True)

    assert after_kill == {newest: LINKED, middle: UNLINKED, oldest: UNLINKED}
    assert (_outcome(rerun), rerun.changes) == (
        [(newest, 4, 0, False, False), (middle, 4, 0, True, True), (oldest, 4, 0, True, True)],
        4,
    )
    assert after_rerun == {newest: LINKED, middle: LINKED, oldest: LINKED}
    assert (third.changes, _state(migrated_engine)) == (0, after_rerun)
    assert _observation_count(migrated_engine) == 12


def _database_path(engine: Engine) -> Path:
    assert engine.url.database is not None
    return Path(engine.url.database)


def _try_to_link(database: Path, receipt_id: int) -> str | None:
    """From a second connection with no busy wait, try to link the receipt's discount; the
    error message, or None when the write went through."""
    with closing(sqlite3.connect(database, timeout=0)) as connection:
        try:
            with connection:
                connection.execute(
                    "UPDATE line_items SET applies_to_position = 0"
                    " WHERE receipt_id = ? AND position = 4",
                    (receipt_id,),
                )
        except sqlite3.OperationalError as error:
            return str(error)
    return None


def test_tc_117_no_writer_interleaves_between_the_backfills_read_and_its_writes(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    uow_factory = sqlite_uow_factory(migrated_engine, FakeClock())
    receipt_id = _store_pre_feature(uow_factory, at(0))
    database = _database_path(migrated_engine)
    second_writer: list[str | None] = []
    writing = [False]
    original = SqliteReceiptRepository.get

    def spying_get(self: SqliteReceiptRepository, wanted: int) -> Receipt | None:
        found = original(self, wanted)
        if writing[0] and wanted == receipt_id:
            second_writer.append(_try_to_link(database, wanted))
        return found

    class Spied(SqliteUnitOfWork):
        def __enter__(self) -> "Spied":
            entered = super().__enter__()
            writing[0] = not self._read_only
            return entered

        def __exit__(self, *exc: object) -> None:
            writing[0] = False
            super().__exit__(*exc)

    def factory(*, read_only: bool = False) -> SqliteUnitOfWork:
        return Spied(migrated_engine, FakeClock(), read_only=read_only)

    monkeypatch.setattr(SqliteReceiptRepository, "get", spying_get)

    backfill = BackfillPrices(factory)(apply=True)

    assert second_writer, "the backfill never re-read the receipt inside its write unit"
    assert second_writer == ["database is locked"] * len(second_writer)
    assert (_outcome(backfill), _state(migrated_engine)) == (
        [(receipt_id, 4, 0, True, True)],
        {receipt_id: LINKED},
    )
    assert _observation_count(migrated_engine) == 4
