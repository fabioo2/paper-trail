"""``BackfillPrices`` on real SQLite: one unit of work per receipt (discounts plan R9, A6).

Each receipt is re-read, attributed and written inside one ``BEGIN IMMEDIATE`` unit, so a
write another process made after the scan is seen (a link kept, a status re-checked), a failure
rolls that receipt back whole, and a rerun completes the rest (idempotent).
"""

import sqlite3
from contextlib import closing
from datetime import datetime
from itertools import count
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from paper_trail.adapters.sqlite.unit_of_work import SqliteUnitOfWork
from paper_trail.application.prices import BackfillPrices, PriceBackfill
from paper_trail.ports.unit_of_work import UnitOfWorkFactory
from tests.factories import (
    at,
)
from tests.fakes.clock import FakeClock
from tests.storage import seed_pre_feature, sqlite_uow_factory
from tests.support import require_id

# (receipt id, discount position, item position, new link, price updated) per discount
type Outcome = list[tuple[int, int, int | None, bool, bool]]
# per receipt: (discount line's applies_to_position, eggs observation's discount and paid)
type State = dict[int, tuple[int | None, int, int]]


class _Killed(BaseException):
    """Stands for the process being killed: not an Exception, so nothing catches it."""


def _store_pre_feature(uow_factory: UnitOfWorkFactory, created_at: datetime) -> int:
    return require_id(seed_pre_feature(uow_factory, created_at=created_at).id)


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


def _write_first(engine: Engine, statement: str) -> UnitOfWorkFactory:
    """Units of work whose first writing unit lets a second connection run ``statement`` just
    before its BEGIN IMMEDIATE: the moment after the scan and before the receipt is re-read."""
    database = _database_path(engine)
    pending = [statement]

    def factory(*, read_only: bool = False) -> SqliteUnitOfWork:
        if not read_only and pending:
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(pending.pop())
        return SqliteUnitOfWork(engine, FakeClock(), read_only=read_only)

    return factory


def test_tc_117_the_backfill_reads_the_receipt_again_inside_its_write_unit(
    migrated_engine: Engine,
) -> None:
    """A link stored by another writer after the scan is seen and kept: no new link, only the
    observation's discount is written."""
    receipt_id = _store_pre_feature(sqlite_uow_factory(migrated_engine, FakeClock()), at(0))
    factory = _write_first(
        migrated_engine,
        "UPDATE line_items SET applies_to_position = 0 WHERE kind = 'discount'",
    )

    backfill = BackfillPrices(factory)(apply=True)

    assert (_outcome(backfill), backfill.links_set) == ([(receipt_id, 4, 0, False, True)], 0)
    assert _state(migrated_engine) == {receipt_id: LINKED}


def test_tc_117_a_receipt_sent_to_review_after_the_scan_is_skipped(
    migrated_engine: Engine,
) -> None:
    """The status is checked again inside the unit: a receipt no longer COMPLETED is left as
    it is."""
    receipt_id = _store_pre_feature(sqlite_uow_factory(migrated_engine, FakeClock()), at(0))
    factory = _write_first(
        migrated_engine, "UPDATE receipts SET status = 'needs_review', stage = 'needs_review'"
    )

    backfill = BackfillPrices(factory)(apply=True)

    assert (_outcome(backfill), _state(migrated_engine)) == ([], {receipt_id: UNLINKED})
